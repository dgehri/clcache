#!/bin/bash

# Get the script directory path
srcPath=/src

# Function to extract package info from conanfile.py
get_package_info() {
    local conanfile="${srcPath}/conan/conanfile.py"

    name=$(grep -Po 'name\s*=\s*"\K[^"]*' "$conanfile")
    version=$(grep -Po 'version\s*=\s*"\K[^"]*' "$conanfile")
    user=$(grep -Po 'user\s*=\s*"\K[^"]*' "$conanfile")
    channel=$(grep -Po 'channel\s*=\s*"\K[^"]*' "$conanfile")
}

# Main function
main() {
    # Get package info
    get_package_info

    # Navigate to clcache/clcache_lib and run pipenv commands
    pushd "$srcPath/clcache/clcache_lib" > /dev/null
    wine pip uninstall -y clcache-lib
    wine pip install -e .
    popd > /dev/null

    # Get Nuitka version and display it
    nuitkaVersion=$(wine python -m nuitka --version)
    echo "Nuitka version: $nuitkaVersion"

    # Run Nuitka
    pushd "$srcPath" > /dev/null
    wine cmd /c \
        python -m nuitka \
        --standalone \
        --plugin-enable=pylint-warnings \
        --no-deployment-flag=self-execution \
        --python-flag="-O" \
        --mingw64 clcache
    popd > /dev/null

    # Export Conan package
    pushd "$srcPath/conan" > /dev/null
    export CONAN_REVISIONS_ENABLED=1
    wine conan export-pkg conanfile.py --force

    # Upload Conan package if confirmed by the user
    if [ "$upload" == "y" ]; then
        wine conan upload "$name/$version@$user/$channel" --all -r globus-conan-local
    else
        echo "Skipping upload. -- if you change your mind, run: wine conan upload $name/$version@$user/$channel --all -r globus-conan-local"
    fi

    popd > /dev/null
}

# Execute main function
main
