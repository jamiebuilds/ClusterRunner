from genty import genty, genty_dataset
from os.path import expanduser, join

from app.common.build_artifact import BuildArtifact
from app.util.conf.configuration import Configuration
from test.framework.base_unit_test_case import BaseUnitTestCase

@genty
class TestBuildArtifact(BaseUnitTestCase):
    def setUp(self):
        super().setUp()
        Configuration['artifact_directory'] = expanduser('~')

    @genty_dataset(
        default=(join(expanduser('~'), '1', 'artifact_2_3'), 1, 2, 3),
        with_nondefault_root=(join('override', '1', 'artifact_2_3'), 1, 2, 3, join('override')),
    )
    def test_atom_artifact_directory_returns_proper_artifact_path(self, expected_path, build_id, subjob_id=None,
                                                                  atom_id=None, result_root=None):
        self.assertEquals(
            expected_path,
            BuildArtifact.atom_artifact_directory(build_id, subjob_id, atom_id, result_root=result_root),
            'The generated atom artifact directory is incorrect.'
        )

    @genty_dataset(
        default=(join(expanduser('~'), '1'), 1),
        with_nondefault_root=(join('override', '1'), 1, join('override')),
    )
    def test_build_artifact_directory_returns_proper_artifact_path(self, expected_path, build_id, result_root=None):
        self.assertEquals(
            expected_path,
            BuildArtifact.build_artifact_directory(build_id, result_root=result_root),
            'The generated build artifact directory is incorrect.'
        )

    @genty_dataset(
        relative_path=('artifact_0_1', 0, 1),
        absolute_path=('/path/to/build/1/artifact_0_1', 0, 1),
    )
    def test_subjob_and_atom_ids_parses_for_properly_formatted_directory(self, artifact_directory, expected_subjob_id,
                                                                         expected_atom_id):
        subjob_id, atom_id = BuildArtifact._subjob_and_atom_ids(artifact_directory)
        self.assertEquals(subjob_id, expected_subjob_id)
        self.assertEquals(atom_id, expected_atom_id)

    @genty_dataset(
        'artifact_0',
        '/full/path/artifact_0',
        'wrong_0_1',
        'artifact_0_',
    )
    def test_subjob_and_atom_ids_raises_value_error_with_incorrect_format(self, incorrect_artifact_directory):
        with self.assertRaises(ValueError):
            BuildArtifact._subjob_and_atom_ids(incorrect_artifact_directory)

