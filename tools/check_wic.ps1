# ASCII-only: probe WIC RAW decoding capability and dump the decoded bitmap to PNG.
# Chinese text is deliberately avoided here (Windows PowerShell 5.1 reads .ps1 as ANSI).
$ErrorActionPreference = 'Continue'
Add-Type -AssemblyName PresentationCore

Write-Output '=== 1) RAW-related app packages ==='
$pkgs = @(Get-AppxPackage -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -match 'Raw|Codec|Imaging' })
if ($pkgs.Count -gt 0) {
    $pkgs | ForEach-Object { Write-Output ("  PKG {0} {1}" -f $_.Name, $_.Version) }
} else {
    Write-Output '  (no matching package; decoder may be a built-in system component)'
}

$file = $env:COGITATOR_RAW_SAMPLE
if (-not $file) { Write-Error '请先设置 COGITATOR_RAW_SAMPLE 指向一个 .CR3 文件'; exit 1 }
Write-Output ''
Write-Output '=== 2) WIC decoder info for a real CR3 ==='
$uri = New-Object System.Uri($file)
$dec = [System.Windows.Media.Imaging.BitmapDecoder]::Create($uri, 'None', 'OnLoad')
$f = $dec.Frames[0]
Write-Output ("  decoder   : {0}" -f $dec.GetType().FullName)
Write-Output ("  frames    : {0}" -f $dec.Frames.Count)
Write-Output ("  size      : {0}x{1}" -f $f.PixelWidth, $f.PixelHeight)
Write-Output ("  format    : {0} ({1} bpp)" -f $f.Format, $f.Format.BitsPerPixel)
$native = @($f.NativePixelFormats | ForEach-Object { "$_/$($_.BitsPerPixel)bpp" })
Write-Output ("  native    : {0}" -f ($native -join ', '))

Write-Output ''
Write-Output '=== 3) dump decoded bitmap to PNG for pixel comparison ==='
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$outDir = [System.IO.Path]::GetFullPath((Join-Path $here '..\.verify'))
$wicPath = Join-Path $outDir 'wic_decode.png'
$conv = New-Object System.Windows.Media.Imaging.FormatConvertedBitmap($f, [System.Windows.Media.PixelFormats]::Bgr24, $null, 0)
$enc = New-Object System.Windows.Media.Imaging.PngBitmapEncoder
$enc.Frames.Add([System.Windows.Media.Imaging.BitmapFrame]::Create($conv))
$fs = [System.IO.File]::Create($wicPath)
$enc.Save($fs)
$fs.Close()
Write-Output ("  saved: {0} ({1:N0} KB)" -f $wicPath, ((Get-Item $wicPath).Length / 1KB))

Write-Output ''
Write-Output '=== 4) pixel comparison (delegated to python) ==='
$env:PYTHONUTF8 = '1'
& python (Join-Path $here 'cmp_wic_embedded.py')
