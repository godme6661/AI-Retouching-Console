# ASCII-only WIC RAW dumper (Windows Imaging Component via WPF).
# Ships inside the package: cogitator/wic_dump.ps1
#
# Writes: 16-byte header (magic "WIC1", int32 width, int32 height, int32 channels) + raw BGR24 pixels.
# Why not PNG: encoding a 24MP PNG costs seconds; raw bytes are far cheaper (measured ~23ms vs seconds).
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File wic_dump.ps1 -Path <raw> -Out <bin> [-MaxSide N]
param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][string]$Out,
    [int]$MaxSide = 0
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName PresentationCore
$sw = [System.Diagnostics.Stopwatch]::StartNew()

$uri = New-Object System.Uri($Path)
$dec = [System.Windows.Media.Imaging.BitmapDecoder]::Create($uri, 'None', 'OnLoad')
$frame = $dec.Frames[0]
$tDecode = $sw.ElapsedMilliseconds

$src = $frame
if ($MaxSide -gt 0 -and ([Math]::Max($frame.PixelWidth, $frame.PixelHeight) -gt $MaxSide)) {
    $scale = $MaxSide / [double]([Math]::Max($frame.PixelWidth, $frame.PixelHeight))
    $src = New-Object System.Windows.Media.Imaging.TransformedBitmap($frame, (New-Object System.Windows.Media.ScaleTransform($scale, $scale)))
}

$bgr = New-Object System.Windows.Media.Imaging.FormatConvertedBitmap($src, [System.Windows.Media.PixelFormats]::Bgr24, $null, 0)
$w = $bgr.PixelWidth
$h = $bgr.PixelHeight
$stride = $w * 3
$buf = New-Object byte[] ($stride * $h)
$bgr.CopyPixels($buf, $stride, 0)
$tPixels = $sw.ElapsedMilliseconds

$fs = [System.IO.File]::Create($Out)
$bw = New-Object System.IO.BinaryWriter($fs)
$bw.Write([char[]]'WIC1')
$bw.Write([int]$w)
$bw.Write([int]$h)
$bw.Write([int]3)
$bw.Write($buf)
$bw.Flush()
$bw.Close()
$fs.Close()
$sw.Stop()

Write-Output ("OK {0} {1} decode={2}ms pixels={3}ms total={4}ms" -f $w, $h, $tDecode, ($tPixels - $tDecode), $sw.ElapsedMilliseconds)
