call venv_py3\Scripts\activate.batcomp  
set PATH=C:\Program Files\Conan\conan;%PATH%

pushd clcache\clcache_lib
pip uninstall -y clcache-lib 
pip install -e .
popd 

python -m nuitka --standalone --plugin-enable=multiprocessing --plugin-enable=pylint-warnings --python-flag="-O" --mingw64 .\clcache
pushd conan
set CONAN_REVISIONS_ENABLED=1

conan export-pkg conanfile.py --force

set USER=dgehri
set CHANNEL=dev
set VERSION=4.4.3ak

conan upload clcache/%VERSION%@%USER%/%CHANNEL% --all -r globus-conan-local

popd
pause

rem Powershell version of the above
rem Path: compile_nuitka.ps1
