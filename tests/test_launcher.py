from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import launcher

class Startup(unittest.TestCase):
    def test_windows_environment_path(self):
        self.assertEqual(launcher.environment_python(Path('program'),True).as_posix(),'program/.venv/Scripts/python.exe')
    def test_existing_environment_skips_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            python=root/'python';python.touch()
            with patch.object(launcher,'ROOT',root),patch.object(launcher,'environment_python',return_value=python),patch.object(launcher.subprocess,'run',return_value=subprocess.CompletedProcess([],0)) as run,patch.object(launcher.subprocess,'call',return_value=0) as call,patch.object(launcher.venv,'EnvBuilder') as builder:
                self.assertEqual(launcher.main(),0)
                builder.assert_not_called()
                self.assertEqual(run.call_count,1)
                call.assert_called_once_with([str(python),'-u',str(root/'app.py')],cwd=str(root))
    def test_first_start_creates_environment_installs_and_launches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);python=root/'missing-python'
            with patch.object(launcher,'ROOT',root),patch.object(launcher,'environment_python',return_value=python),patch.object(launcher.subprocess,'run',side_effect=[subprocess.CompletedProcess([],1),subprocess.CompletedProcess([],0)]) as run,patch.object(launcher.subprocess,'call',return_value=0),patch.object(launcher.venv,'EnvBuilder') as builder:
                self.assertEqual(launcher.main(),0)
                builder.return_value.create.assert_called_once_with(root/'.venv')
                self.assertIn('install',run.call_args_list[1].args[0])
    def test_failed_install_does_not_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);python=root/'python';python.touch()
            with patch.object(launcher,'ROOT',root),patch.object(launcher,'environment_python',return_value=python),patch.object(launcher.subprocess,'run',side_effect=[subprocess.CompletedProcess([],1),subprocess.CalledProcessError(1,'pip')]),patch.object(launcher.subprocess,'call') as call:
                self.assertEqual(launcher.main(),1)
                call.assert_not_called()

if __name__=='__main__':unittest.main()
