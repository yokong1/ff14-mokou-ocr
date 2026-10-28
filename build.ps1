# 编译 OCR 服务宿主 (需要 .NET Framework 4.x + Windows SDK 的 Windows.winmd)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$src  = Join-Path $root 'src\ocr_host.cs'
$out  = Join-Path $root 'bin\ocr_host.exe'
New-Item -ItemType Directory -Force -Path (Join-Path $root 'bin') | Out-Null

$csc = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path $csc)) { $csc = 'C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe' }
if (-not (Test-Path $csc)) { throw "找不到 csc.exe (.NET Framework 4.x)" }

$winmd = Get-ChildItem 'C:\Program Files (x86)\Windows Kits\10\UnionMetadata' -Recurse -Filter 'Windows.winmd' -ErrorAction SilentlyContinue |
         Where-Object { $_.FullName -notmatch '\\Facade\\' } |
         Sort-Object Length -Descending | Select-Object -First 1
if (-not $winmd) { throw "找不到 Windows.winmd (Windows SDK)" }
$wr = (Get-ChildItem 'C:\Windows\Microsoft.NET\assembly\GAC_MSIL\System.Runtime.WindowsRuntime' -Recurse -Filter '*.dll' |
       Select-Object -First 1).FullName
if (-not $wr) { throw "找不到 System.Runtime.WindowsRuntime.dll" }
$fac = 'C:\Program Files (x86)\Reference Assemblies\Microsoft\Framework\.NETFramework\v4.8\Facades\System.Runtime.dll'
$refs = @("/r:$($winmd.FullName)", "/r:$wr")
if (Test-Path $fac) { $refs += "/r:$fac" }

& $csc /nologo /target:exe /optimize+ /platform:anycpu "/out:$out" @refs $src 2>&1 |
    Where-Object { $_ -notmatch 'warning CS1701' } | ForEach-Object { Write-Host $_ }
if (Test-Path $out) { Write-Host "OK -> $out" } else { throw "编译失败" }
