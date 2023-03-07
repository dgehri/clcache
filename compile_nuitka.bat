call venv_py3\Scripts\activate.bat
set PATH=C:\Program Files\Conan\conan;%PATH%

pushd clcache\clcache_lib
pip uninstall -y clcache-lib 
pip install -e .
popd 

python -m nuitka --standalone --plugin-enable=multiprocessing --plugin-enable=pylint-warnings --python-flag="-O" --mingw64 .\clcache
pushd conan
set CONAN_REVISIONS_ENABLED=1
conan export-pkg conanfile.py --force
rem conan upload clcache/* --all -r globus-conan-local
popd
pause

