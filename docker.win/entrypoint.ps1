$MainFunction = {
    $packageInfo = GetPackageInfo
    $name = $packageInfo.name
    $version = $packageInfo.version
    $user = $packageInfo.user
    $channel = $packageInfo.channel
    
    # Ask your user if upload is desired
    $upload = Read-Host "Upload $name/$version@$user/$channel to globus-conan-local? (y/n)"

    python -m pip install --use-pep517 -r clcache\requirements.txt

    $env:PYTHONPATH = "C:\src\clcache\clcachelib\clcachelib"

    Push-Location clcache\clcachelib
    python -m pip install .
    Pop-Location

    # Print Nuitka version to stdout, prefixed with "Nuitka version: "
    $nuitkaVersion = python -m nuitka --version
    Write-Output "Nuitka version: $nuitkaVersion"

    python -m nuitka `
        --standalone `
        --plugin-enable=pylint-warnings `
        --no-deployment-flag=self-execution `
        --python-flag="-O" `
        --mingw64 `
        --remove-output `
        --disable-ccache `
        --report=clcache.dist\report.xml `
        .\clcache

    # Test run the compiled executable and check the exit code
    $exitCode = .\clcache.dist\clcache.exe
    if ($exitCode -ne 0) {
        Write-Error "The compiled executable returned a non-zero exit code: $exitCode"
        exit 1
    }

    Push-Location conan
    $env:CONAN_REVISIONS_ENABLED = 1
    
    if ($upload -eq "y") {
        conan remote add globus-conan-local https://conan-us.globusmedical.com/artifactory/api/conan/globus-conan-local
        conan user -p -r globus-conan-local admin
        conan export-pkg conanfile.py --force
        conan upload "$name/$version@$user/$channel" --all -r globus-conan-local
    }
    
    Pop-Location
}

function GetPackageInfo {

    # Read the content of the file
    $fileContent = Get-Content "conan/conanfile.py" -Raw

    # Define regular expressions to match the desired information
    $nameRegex = 'name\s*=\s*"([^"]+)"'
    $versionRegex = 'version\s*=\s*"([^"]+)"'
    $userRegex = 'user\s*=\s*"([^"]+)"'
    $channelRegex = 'channel\s*=\s*"([^"]+)"'

    # Apply regexes to file content and capture the matched groups
    if ($fileContent -match $nameRegex) {
        $name = $Matches[1]
    }
    if ($fileContent -match $versionRegex) {
        $version = $Matches[1]
    }
    if ($fileContent -match $userRegex) {
        $user = $Matches[1]
    }
    if ($fileContent -match $channelRegex) {
        $channel = $Matches[1]
    }

    return @{
        name = $name
        version = $version
        user = $user
        channel = $channel
    }
}

& $MainFunction

