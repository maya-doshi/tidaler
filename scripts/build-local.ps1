param(
    [string]$Python = "$PSScriptRoot\..\.venv\Scripts\python.exe"
)
$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path "$PSScriptRoot\..").Path
Push-Location $projectRoot
try {
    function Invoke-Python {
        param([string[]]$Arguments)
        & $Python @Arguments
        if ($LASTEXITCODE -ne 0) { throw "Python command failed: $Arguments" }
    }
    Invoke-Python -Arguments @("-m", "pip", "install", "--no-deps", "-e", ".")
    Invoke-Python -Arguments @("-m", "build")
    New-Item -ItemType Directory -Force build | Out-Null
    Invoke-Python -Arguments @("-m", "PyInstaller", "--noconfirm", "--onefile", "--console",
        "--name", "tidaler", "--distpath", "dist", "--workpath", "build\pyinstaller",
        "--specpath", "build", "--paths", ".", "--copy-metadata", "tidaler",
        "--exclude-module", "tidaler.gui", "--exclude-module", "PySide6", "scripts\run_cli.py")
    $version = (& $Python -c 'from tidaler import __version__; print(__version__)').Trim()
    if ($LASTEXITCODE -ne 0) { throw "Cannot read release version" }
    $reported = (& .\dist\tidaler.exe --version).Trim()
    if ($LASTEXITCODE -ne 0 -or $reported -ne $version) { throw "Executable version smoke test failed" }
    & .\dist\tidaler.exe isrc --help
    if ($LASTEXITCODE -ne 0) { throw "Executable ISRC help smoke test failed" }
    & .\dist\tidaler.exe worker --help
    if ($LASTEXITCODE -ne 0) { throw "Executable worker help smoke test failed" }
    $archive = "dist\tidaler-$version-windows-x64-cli.zip"
    Compress-Archive -LiteralPath "dist\tidaler.exe", "LICENSE", "docs\local-release.md" -DestinationPath $archive -Force
    & $Python -m pip freeze | Set-Content -Encoding utf8 "dist\build-environment.txt"
    if ($LASTEXITCODE -ne 0) { throw "Cannot record build environment" }
    $files = @("dist\tidaler.exe", $archive,
        "dist\tidaler-$version-py3-none-any.whl", "dist\tidaler-$version.tar.gz")
    $hashes = foreach ($file in $files) {
        $hash = Get-FileHash -LiteralPath $file -Algorithm SHA256
        "$($hash.Hash.ToLower())  $([IO.Path]::GetFileName($file))"
    }
    $hashes | Set-Content -Encoding ascii "dist\SHA256SUMS.txt"
    Write-Output "Local release created in $projectRoot\dist"
}
finally {
    Pop-Location
}
