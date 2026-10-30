# 编译成功后才替换宿主；失败时保留上一个可用版本。
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$src = Join-Path $root 'src\ocr_host.cs'
$out = Join-Path $root 'bin\ocr_host_0_9_2.exe'
$temp = Join-Path $root 'bin\ocr_host_0_9_2.build.tmp.exe'
New-Item -ItemType Directory -Force -Path (Join-Path $root 'bin') | Out-Null
$csc = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $csc)) { $csc = 'C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe' }
if (-not (Test-Path -LiteralPath $csc)) { throw '找不到 .NET Framework C# 编译器' }
$metadata = Get-ChildItem 'C:\Program Files (x86)\Windows Kits\10\UnionMetadata' -Recurse -Filter 'Windows.winmd' -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -notmatch '\\Facade\\' } | Sort-Object Length -Descending | Select-Object -First 1
if (-not $metadata) {
    $metadata = Get-ChildItem 'C:\Windows\System32\WinMetadata' -Filter '*.winmd' -ErrorAction SilentlyContinue
}
if (-not $metadata) { throw '找不到 Windows WinRT 元数据，请安装 Windows SDK' }
$refs = @($metadata | ForEach-Object { '/r:' + $_.FullName })
foreach ($assembly in @('System.Runtime.WindowsRuntime', 'System.Runtime')) {
    $dll = Get-ChildItem "C:\Windows\Microsoft.NET\assembly\GAC_MSIL\$assembly" -Recurse -Filter "$assembly.dll" -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $dll) { throw "找不到 $assembly.dll" }
    $refs += '/r:' + $dll.FullName
}
try {
    & $csc /nologo /target:exe /optimize+ /platform:anycpu "/out:$temp" @refs $src
    if ($LASTEXITCODE -ne 0) { throw "编译失败，退出码 $LASTEXITCODE；原宿主已保留" }
    Move-Item -LiteralPath $temp -Destination $out -Force
    Write-Host "OK -> $out"
}
finally {
    if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Force }
}
