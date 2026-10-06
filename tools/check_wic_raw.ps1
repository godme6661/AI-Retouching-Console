# 测：本机 WIC/WPF 能否解码 CR3（未安装 Raw Image Extension 时应当失败）
$ErrorActionPreference = "Continue"
Add-Type -AssemblyName PresentationCore
$file = $env:COGITATOR_RAW_SAMPLE
if (-not $file) { Write-Error "请先设置 COGITATOR_RAW_SAMPLE 指向一个 .CR3 文件"; exit 1 }
Write-Output ("file exists: {0}" -f (Test-Path $file))
try {
    $uri = New-Object System.Uri($file)
    $dec = [System.Windows.Media.Imaging.BitmapDecoder]::Create($uri, "None", "OnLoad")
    $f = $dec.Frames[0]
    Write-Output ("WIC decode OK: {0}x{1}  pixelformat={2}  bpp={3}" -f $f.PixelWidth, $f.PixelHeight, $f.Format, $f.Format.BitsPerPixel)
} catch {
    $msg = $_.Exception.Message -replace "`r?`n", " "
    Write-Output ("WIC decode FAILED: {0}" -f $msg)
    if ($_.Exception.InnerException) {
        $inner = $_.Exception.InnerException.Message -replace "`r?`n", " "
        Write-Output ("  inner: {0}" -f $inner)
    }
}
Write-Output ""
Write-Output "--- registered WIC components (RAW related?) ---"
$names = @()
$wic = "HKLM:\SOFTWARE\Microsoft\Windows Imaging Component\Components"
if (Test-Path $wic) {
    Get-ChildItem $wic -ErrorAction SilentlyContinue | ForEach-Object {
        $p = Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue
        if ($p.FriendlyName) { $names += $p.FriendlyName }
    }
}
Write-Output ("total codecs: {0}" -f $names.Count)
$raw = @($names | Where-Object { $_ -match "RAW|Canon|Nikon|Sony|CR3|DNG" })
if ($raw.Count -gt 0) { $raw | ForEach-Object { Write-Output ("  RAW codec: " + $_) } }
else { Write-Output "  no RAW codec registered (consistent with Raw Image Extension not installed)" }
