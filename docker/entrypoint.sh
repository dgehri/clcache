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
    # Ask if the user wants to upload the Conan package
    read -p "Do you want to upload the Conan package? (y/n): " upload
    if [ "$upload" == "y" ]; then
        echo "Conan package will be uploaded."
    else
        echo "Conan package will not be uploaded."
    fi


    # Get package info
    get_package_info

    # Add clcache/clcachelib/clcachelib to the PYTHONPATH
    wine setx PYTHONPATH "Z:\\src\\clcache\\clcachelib\\clcachelib"

    # Install additional dependencies from requirements.txt
    wine pip install -r "Z:\\src\\clcache\\requirements.txt" || exit 1

    # Install clcache library
    pushd "$srcPath/clcache/clcachelib" > /dev/null
    wine pip install . || exit 1
    popd > /dev/null

    # Get Nuitka version and display it
    nuitkaVersion=$(wine python -m nuitka --version)
    echo "Nuitka version: $nuitkaVersion"

    # Run Nuitka
    pushd "$srcPath" > /dev/null
    unbuffer wine \
        python -m nuitka \
        --standalone \
        --plugin-enable=pylint-warnings \
        --no-deployment-flag=self-execution \
        --disable-ccache \
        --remove-output \
        --report=/src/clcache.dist/report.xml \
        --python-flag="-O" \
        --mingw64 clcache; \
        wineserver -w &&
    
    popd > /dev/null

    # Export Conan package
    pushd "$srcPath/conan" > /dev/null
    export CONAN_REVISIONS_ENABLED=1

    # Upload Conan package if confirmed by the user
    if [ "$upload" == "y" ]; then
        wine conan remote add globus-conan-local https://conan-us.globusmedical.com/artifactory/api/conan/globus-conan-local
        # if CONAN_TOKEN is set, use it to authenticate
        if [ -n "$CONAN_TOKEN" ]; then
            wine conan user -p "$CONAN_TOKEN" -r globus-conan-local admin
        else
            wine conan user -p -r globus-conan-local admin
        fi
        wine conan export-pkg conanfile.py --force
        wine conan upload "$name/$version@$user/$channel" --all -r globus-conan-local
    else
        echo "Conan package not uploaded."
    fi

    popd > /dev/null
}

# Execute main function
main
