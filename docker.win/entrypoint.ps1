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

    Push-Location conan
    $env:CONAN_REVISIONS_ENABLED = 1

    # Export Conan package (if this fails, ensure you have conan 1.x)
    conan export-pkg conanfile.py --force

    if ($upload -eq "y") {
        # Upload Conan package
        conan upload "$name/$version@$user/$channel" --all -r globus-conan-local
    } else {
        Write-Output "Skipping upload. -- if you change your mind, run: conan upload $name/$version@$user/$channel --all -r globus-conan-local"
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

