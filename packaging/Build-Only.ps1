<#
.SYNOPSIS
  시험용 빌드: 버전·CHANGELOG·git을 건드리지 않고 지금 코드 그대로 exe와 설치 파일만 만든다.

.DESCRIPTION
  1) packaging/build.py      -> dist/CardTranslater/ (자체 시험 포함, -NoTest로 끔)
  2) Inno Setup(ISCC)        -> dist/CardTranslater-Setup-<버전>-<날짜-시각>-test.exe
     정식 설치 파일(CardTranslater-Setup-<버전>-<날짜>.exe)과 이름이 달라 덮어쓰지 않는다.
  글꼴 설치 파일은 만들지 않는다.

.EXAMPLES
  .\packaging\Build-Only.ps1              # 빌드 + 자체 시험 + 설치 파일
  .\packaging\Build-Only.ps1 -NoTest      # 자체 시험 건너뜀
  .\packaging\Build-Only.ps1 -NoInstaller # dist/CardTranslater/ 만 (설치 파일 없이)
#>
param(
    [switch]$NoTest,
    [switch]$NoInstaller
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Root '.venv\Scripts\python.exe'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = 'utf-8'

if (-not (Test-Path $Py)) { Write-Host "파이썬이 없습니다: $Py" -ForegroundColor Red; exit 1 }
$distDir = (Join-Path $Root 'dist\CardTranslater') + '\'  # 빌드가 덮어쓰는 폴더만 (dist의 복사본 폴더는 괜찮다)
$running = Get-Process -Name CardTranslater -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.StartsWith($distDir, [System.StringComparison]::OrdinalIgnoreCase) }
if ($running) {  # 설치된 프로그램(Program Files 등)은 상관없고, dist 안의 exe가 켜져 있으면 덮어쓸 수 없다
    Write-Host 'dist의 CardTranslater.exe가 켜져 있습니다. 끄고 다시 실행하세요.' -ForegroundColor Red
    exit 1
}

Push-Location $Root
try {
    $Version = (& $Py -c "from app.version import VERSION; print(VERSION)").Trim()
    Write-Host "[build-only] 버전 $Version (버전·CHANGELOG·git은 바꾸지 않음)" -ForegroundColor Cyan

    $buildArgs = @('packaging\build.py')
    if ($NoTest) { $buildArgs += '--no-test' }
    & $Py @buildArgs
    if ($LASTEXITCODE -ne 0) { Write-Host '[build-only] 빌드 실패' -ForegroundColor Red; exit $LASTEXITCODE }

    if ($NoInstaller) {
        Write-Host "[build-only] 완료: $Root\dist\CardTranslater\CardTranslater.exe" -ForegroundColor Green
        exit 0
    }

    $iscc = @(
        (Get-Command iscc -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source),
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
        'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
        'C:\Program Files\Inno Setup 6\ISCC.exe'
    ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if (-not $iscc) { Write-Host 'Inno Setup이 없습니다: winget install JRSoftware.InnoSetup' -ForegroundColor Red; exit 1 }

    $name = "$Version-$(Get-Date -Format 'yyyyMMdd-HHmm')-test"
    & $iscc "/DAppVersion=$Version" "/DSetupName=$name" (Join-Path $Root 'packaging\installer.iss')
    if ($LASTEXITCODE -ne 0) { Write-Host '[build-only] 설치 파일 만들기 실패' -ForegroundColor Red; exit $LASTEXITCODE }

    $out = Join-Path $Root "dist\CardTranslater-Setup-$name.exe"
    $mb = [math]::Round((Get-Item $out).Length / 1MB, 1)
    Write-Host "[build-only] 완료: $out ($mb MB)" -ForegroundColor Green
}
finally {
    Pop-Location
}
