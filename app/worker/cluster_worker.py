from enum import Enum
from queue import Queue
import sys
import sched
import time

import requests

from app.common.cluster_service import ClusterService
from app.project_type.project_type import SetupFailureError
from app.worker.subjob_executor import SubjobExecutor
from app.util import analytics, log, util
from app.util.conf.configuration import Configuration
from app.util.exceptions import BadRequestError
from app.util.network import Network
from app.util.safe_thread import SafeThread
from app.util.secret import Secret
from app.util.session_id import SessionId
from app.util.single_use_coin import SingleUseCoin
from app.util.unhandled_exception_handler import UnhandledExceptionHandler
from app.util.url_builder import UrlBuilder


class ClusterWorker(ClusterService):

    API_VERSION = 'v1'

    def __init__(self, port, host, num_executors=10):
        """
        :param port: The port number the worker service is running on
        :type port: int
        :param host: The hostname at which the worker is reachable
        :type host: str
        :param num_executors: The number of executors this worker should operate with -- this determines how many
            concurrent subjobs the worker can execute.
        :type num_executors: int
        """
        self.port = port
        self.host = host
        self.is_alive = True
        self._worker_id = None
        self._num_executors = num_executors
        self._logger = log.get_logger(__name__)

        self._idle_executors = Queue(maxsize=num_executors)
        self.executors_by_id = {}
        for executor_id in range(num_executors):
            executor = SubjobExecutor(executor_id)
            self._idle_executors.put(executor)
            self.executors_by_id[executor_id] = executor

        self._manager_url = None
        self._network = Network(min_connection_poolsize=num_executors)
        self._manager_api = None  # wait until we connect to a manager first

        self._project_type = None  # this will be instantiated during build setup
        self._current_build_id = None
        self._build_teardown_coin = None
        self._base_executor_index = None

        # Configure heartbeat
        self._heartbeat_failure_count = 0
        self._heartbeat_failure_threshold = Configuration['heartbeat_failure_threshold']
        self._heartbeat_interval = Configuration['heartbeat_interval']
        self._hb_scheduler = sched.scheduler()

    def start_heartbeat_thread(self):
        self._logger.info('Heartbeat will run every {} seconds'.format(self._heartbeat_interval))
        SafeThread(target=self._start_heartbeat, name='HeartbeatThread', daemon=True).start()

    def _start_heartbeat(self):
        self._hb_scheduler.enter(0, 0, self._run_heartbeat)
        self._hb_scheduler.run()

    def _run_heartbeat(self):
        try:
            self._send_heartbeat_to_manager()
            self._heartbeat_failure_count = 0
        except (requests.ConnectionError, requests.Timeout):
            self._heartbeat_failure_count += 1
            if self._heartbeat_failure_count >= self._heartbeat_failure_threshold:
                self._logger.error('Manager is not responding to heartbeats')

                # TODO: Right now the worker simply dies when it does not hear back from manager. The next step would
                # be to try to reconnect to manager at this point. In future the heartbeat and connect_to_manager
                # methods can combined into one. This combined method will behave differently based on current state.
                self.kill()

        self._hb_scheduler.enter(self._heartbeat_interval, 0, self._run_heartbeat)

    def _send_heartbeat_to_manager(self):
        heartbeat_url = self._manager_api.url('worker', self._worker_id, 'heartbeat')
        self._network.post_with_digest(heartbeat_url, request_params={'worker': {'heartbeat': True}},
                                       secret=Secret.get())

    def api_representation(self):
        """
        Gets a dict representing this resource which can be returned in an API response.
        :rtype: dict [str, mixed]
        """
        executors_representation = [executor.api_representation() for executor in self.executors_by_id.values()]
        return {
            'is_alive': self.is_alive,
            'manager_url': self._manager_url,
            'current_build_id': self._current_build_id,
            'worker_id': self._worker_id,
            'executors': executors_representation,
            'session_id': SessionId.get(),
        }

    def get_status(self):
        """
        Just returns a dumb message and prints it to the console.
        """
        return 'Worker service is up. <Port: {}>'.format(self.port)

    def setup_build(self, build_id, project_type_params, build_executor_start_index):
        """
        Usually called once per build to do build-specific setup. Will block any subjobs from executing until setup
        completes. The actual setup is performed on another thread and will unblock subjobs (via an Event) once it
        finishes.

        :param build_id: The id of the build to run setup on
        :type build_id: int
        :param project_type_params: The parameters that define the project_type this build will execute in
        :type project_type_params: dict
        :param build_executor_start_index: How many executors have alreayd been allocated on other workers for
        this build
        :type build_executor_start_index: int
        """
        self._logger.info('Executing setup for build {} (type: {}).', build_id, project_type_params.get('type'))
        self._current_build_id = build_id
        self._build_teardown_coin = SingleUseCoin()  # protects against build_teardown being executed multiple times

        # create an project_type instance for build-level operations
        self._project_type = util.create_project_type(project_type_params)

        # verify all executors are idle
        if not self._idle_executors.full():
            raise RuntimeError('Worker tried to setup build but not all executors are idle. ({}/{} executors idle.)'
                               .format(self._idle_executors.qsize(), self._num_executors))

        # Collect all the executors to pass to project_type.fetch_project(). This will create a new project_type for
        # each executor (for subjob-level operations).
        executors = list(self._idle_executors.queue)
        SafeThread(
            target=self._async_setup_build,
            name='Bld{}-Setup'.format(build_id),
            args=(executors, project_type_params, build_executor_start_index)
        ).start()

    def _async_setup_build(self, executors, project_type_params, build_executor_start_index):
        """
        Called from setup_build(). Do asynchronous setup for the build so that we can make the call to setup_build()
        non-blocking.

        :type executors: list[SubjobExecutor]
        :type project_type_params: dict
        :type build_executor_start_index: int
        """
        self._base_executor_index = build_executor_start_index
        try:
            self._project_type.fetch_project()
            for executor in executors:
                executor.configure_project_type(project_type_params)
            self._project_type.run_job_config_setup()

        except SetupFailureError as ex:
            self._logger.error(ex)
            self._logger.info('Notifying manager that build setup has failed for build {}.', self._current_build_id)
            self._notify_manager_of_state_change(WorkerState.SETUP_FAILED)

        else:
            self._logger.info('Notifying manager that build setup is complete for build {}.', self._current_build_id)
            self._notify_manager_of_state_change(WorkerState.SETUP_COMPLETED)

    def teardown_build(self, build_id=None):
        """
        Called at the end of each build on each worker before it reports back to the manager that it is idle again.

        :param build_id: The build id to teardown -- this parameter is used solely for correctness checking of the
            manager, to make sure that the manager is not erroneously sending teardown commands for other builds.
        :type build_id: int | None
        """
        if self._current_build_id is None:
            raise BadRequestError('Tried to teardown a build but no build is active on this worker.')

        if build_id is not None and build_id != self._current_build_id:
            raise BadRequestError('Tried to teardown build {}, '
                                  'but worker is running build {}!'.format(build_id, self._current_build_id))
        SafeThread(
            target=self._async_teardown_build,
            name='Bld{}-Teardwn'.format(build_id)
        ).start()

    def _async_teardown_build(self):
        """
        Called from teardown_build(). Do asynchronous teardown for the build so that we can make the call to
        teardown_build() non-blocking. Also take care of posting back to the manager when teardown is complete.
        """
        self._do_build_teardown_and_reset()
        while not self._idle_executors.full():
            time.sleep(1)
        self._send_manager_idle_notification()

    def _do_build_teardown_and_reset(self, timeout=None):
        """
        Kill any currently running subjobs. Run the teardown_build commands for the current build (with an optional
        timeout). Clear attributes related to the currently running build.

        :param timeout: A maximum time in seconds to allow the teardown process to run before killing
        :type timeout: int | None
        """
        # Kill all subjob executors' processes. This only has an effect if we are tearing down before a build completes.
        for executor in self.executors_by_id.values():
            executor.kill()

        # Order matters! Spend the coin if it has been initialized.
        if not self._build_teardown_coin or not self._build_teardown_coin.spend() or not self._project_type:
            return  # There is no build to tear down or teardown is already in progress.

        self._logger.info('Executing teardown for build {}.', self._current_build_id)
        # todo: Catch exceptions raised during teardown_build so we don't skip notifying manager of idle/disconnect.
        self._project_type.teardown_build(timeout=timeout)
        self._logger.info('Build teardown complete for build {}.', self._current_build_id)
        self._current_build_id = None
        self._project_type = None
        self._base_executor_index = None

    def _send_manager_idle_notification(self):
        if not self._is_manager_responsive():
            self._logger.notice('Could not post idle notification to manager because manager is unresponsive.')
            return

        # Notify manager that this worker is finished with teardown and ready for a new build.
        self._logger.info('Notifying manager that this worker is ready for new builds.')
        self._notify_manager_of_state_change(WorkerState.IDLE)

    def _disconnect_from_manager(self):
        """
        Perform internal bookkeeping, as well as notify the manager, that this worker is disconnecting itself
        from the worker pool.
        """
        self.is_alive = False

        if not self._is_manager_responsive():
            self._logger.notice('Could not post disconnect notification to manager because manager is unresponsive.')
            return

        # Notify manager that this worker is shutting down and should not receive new builds.
        self._logger.info('Notifying manager that this worker is disconnecting.')
        self._notify_manager_of_state_change(WorkerState.DISCONNECTED)

    def connect_to_manager(self, manager_url=None):
        """
        Notify the manager that this worker exists.

        :param manager_url: The URL of the manager service. If none specified, defaults to localhost:43000.
        :type manager_url: str | None
        """
        self.is_alive = True
        self._manager_url = manager_url or 'localhost:43000'
        self._manager_api = UrlBuilder(self._manager_url)
        connect_url = self._manager_api.url('worker')
        data = {
            'worker': '{}:{}'.format(self.host, self.port),
            'num_executors': self._num_executors,
            'session_id': SessionId.get()
        }
        response = self._network.post(connect_url, data=data)
        self._worker_id = int(response.json().get('worker_id'))
        self._logger.info('Worker {}:{} connected to manager on {}.', self.host, self.port, self._manager_url)

        # We disconnect from the manager before build_teardown so that the manager stops sending subjobs. (Teardown
        # callbacks are executed in the reverse order that they're added, so we add the build_teardown callback first.)
        UnhandledExceptionHandler.singleton().add_teardown_callback(self._do_build_teardown_and_reset, timeout=30)
        UnhandledExceptionHandler.singleton().add_teardown_callback(self._disconnect_from_manager)

    def _is_manager_responsive(self):
        """
        Ping the manager to check if it is still alive. Code using this method should treat the return value as a
        *probable* truth since the state of the manager can change at any time. This method is not a replacement for
        error handling.

        :return: Whether the manager is responsive or not
        :rtype: bool
        """
        # todo: This method repeats some logic we have in the deployment code (checking a service). We should DRY it up.
        is_responsive = True
        try:
            self._network.get(self._manager_api.url())
        except (requests.ConnectionError, requests.Timeout):
            is_responsive = False

        return is_responsive

    def start_working_on_subjob(self, build_id, subjob_id, atomic_commands):
        """
        Begin working on a subjob with the given build id and subjob id. This just starts the subjob execution
        asynchronously on a separate thread.

        :type build_id: int
        :type subjob_id: int
        :type atomic_commands: list[str]
        :return: The text to return in the API response.
        :rtype: dict[str, int]
        """
        if build_id != self._current_build_id:
            raise BadRequestError('Attempted to start subjob {} for build {}, '
                                  'but current build id is {}.'.format(subjob_id, build_id, self._current_build_id))

        # get idle executor from queue to claim it as in-use (or block until one is available)
        executor = self._idle_executors.get()

        # Start a thread to execute the job (after waiting for setup to complete)
        SafeThread(
            target=self._execute_subjob,
            args=(build_id, subjob_id, executor, atomic_commands),
            name='Bld{}-Sub{}'.format(build_id, subjob_id),
        ).start()

        self._logger.info('Worker ({}:{}) has received subjob. (Build {}, Subjob {})', self.host, self.port, build_id,
                          subjob_id)
        return {'executor_id': executor.id}

    def _execute_subjob(self, build_id, subjob_id, executor, atomic_commands):
        """
        This is the method for executing a subjob asynchronously. This performs the work required by executing the
        specified command, then does a post back to the manager results endpoint to signal that the work is done.

        :type build_id: int
        :type subjob_id: int
        :type executor: SubjobExecutor
        :type atomic_commands: list[str]
        """
        subjob_event_data = {'build_id': build_id, 'subjob_id': subjob_id, 'executor_id': executor.id}

        analytics.record_event(analytics.SUBJOB_EXECUTION_START, **subjob_event_data)
        results_file = executor.execute_subjob(build_id, subjob_id, atomic_commands, self._base_executor_index)
        analytics.record_event(analytics.SUBJOB_EXECUTION_FINISH, **subjob_event_data)

        results_url = self._manager_api.url('build', build_id, 'subjob', subjob_id, 'result')
        data = {
            'worker': '{}:{}'.format(self.host, self.port),
            'metric_data': {'executor_id': executor.id},
        }
        files = {'file': ('payload', open(results_file, 'rb'), 'application/x-compressed')}

        self._idle_executors.put(executor)  # work is done; mark executor as idle
        resp = self._network.post(results_url, data=data, files=files)
        if resp.ok:
            self._logger.info('Build {}, Subjob {} completed and sent results to manager.', build_id, subjob_id)
        else:
            self._logger.error(
                ('Build {}, Subjob {} encountered an error when sending results to manager.'
                 '\n\tStatus Code {}\n\t{}').format(build_id, subjob_id, resp.status_code, resp.text))

    def _notify_manager_of_state_change(self, new_state):
        """
        Send a state notification to the manager. This is used to notify the manager of events occurring on the worker
        related to build execution progress.

        :type new_state: WorkerState
        """
        state_url = self._manager_api.url('worker', self._worker_id)
        self._network.put_with_digest(state_url, request_params={'worker': {'state': new_state}},
                                      secret=Secret.get(), error_on_failure=True)

    def kill(self):
        """
        Exits without error.
        """
        sys.exit(0)


class WorkerState(str, Enum):
    """
    An enum of possible worker states. Also inherits from string to allow comparisons with other strings (which is
    useful when including these values in API responses).
    """
    DISCONNECTED = 'DISCONNECTED'  # The manager is not in communication with the worker.
    SHUTDOWN = 'SHUTDOWN'  # The worker will not accept additional builds, and will disconnect when finished.
    IDLE = 'IDLE'  # The worker is waiting for a build.
    SETUP_COMPLETED = 'SETUP_COMPLETE'  # The worker has completed a build's setup and is waiting for subjobs.
    SETUP_FAILED = 'SETUP_FAILED'  # A build's setup did not complete successfully, the worker is now stuck.