"""Bootstrap with private venv. No admin rights or PowerShell policy changes."""
from pathlib import Path
import os
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parent

def environment_python(root, windows=None):
    if windows is None:
        windows = os.name == 'nt'
    return root / '.venv' / ('Scripts/python.exe' if windows else 'bin/python')

def main():
    if sys.version_info < (3, 9, 2):
        print('Wymagany Python 3.9.2 lub nowszy. Zainstaluj Python z python.org.')
        return 1
    python = environment_python(ROOT)
    print('CreativeCrafts Faktury - Windows 0.7', flush=True)
    print('Pierwsze uruchomienie przygotuje biblioteke PDF. Potrzebny internet.', flush=True)
    try:
        if not python.is_file():
            print('Przygotowuje srodowisko programu...', flush=True)
            venv.EnvBuilder(with_pip=True).create(ROOT / '.venv')
        check = subprocess.run([str(python), '-c', 'import reportlab, lxml, cryptography; assert cryptography.__version__ == "50.0.2"; assert reportlab.Version == "4.4.9"; assert lxml.__version__ == "6.1.1"'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if check.returncode:
            print('Instaluje biblioteke PDF...', flush=True)
            subprocess.run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check',
                            '-r', str(ROOT / 'requirements.txt')], cwd=str(ROOT), check=True)
        print('Otwieram lokalny panel. Pozostaw to okno otwarte.', flush=True)
        return subprocess.call([str(python), '-u', str(ROOT / 'app.py')], cwd=str(ROOT))
    except KeyboardInterrupt:
        return 0
    except (OSError, subprocess.CalledProcessError) as exc:
        print('\nNie udalo sie uruchomic programu.', flush=True)
        print('Sprawdz internet, dostep do zapisu w folderze i instalacje Python.', flush=True)
        print('Jesli przenosiles folder programu, usun tylko folder .venv i uruchom ponownie.', flush=True)
        print('Nie usuwaj bazy danych z folderu LocalAppData.', flush=True)
        print(str(exc), flush=True)
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
