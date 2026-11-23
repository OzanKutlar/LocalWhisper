# LocalWhisper screen capture helper (Windows PowerShell 5.1).
# Modes:
#   ocr         Capture a region (or the primary screen when W/H are 0), run Windows OCR and
#               print every text line with its bounding box in physical screen pixels.
#   preview     Capture a region, outline the click target, mark the click point and save a
#               small JPEG to OutPath.
#   screenshot  Capture a clean region (or full primary screen), resize to MaxWidth and save
#               a JPEG to OutPath.
#   load_image  Load an existing image file from InPath, resize to MaxWidth and save a JPEG
#               to OutPath.
# Always prints exactly one compact JSON object on stdout.

param(
    [string]$Mode = 'ocr',
    [int]$X = 0,
    [int]$Y = 0,
    [int]$W = 0,
    [int]$H = 0,
    [int]$Scale = 1,
    [int]$BoxX = 0,
    [int]$BoxY = 0,
    [int]$BoxW = 0,
    [int]$BoxH = 0,
    [int]$PointX = -100000,
    [int]$PointY = -100000,
    [int]$MaxWidth = 720,
    [int]$Quality = 80,
    [string]$InPath = '',
    [string]$OutPath = ''
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$MaxCaptureW = 7680
$MaxCaptureH = 4320
$WinRtTimeoutMs = 8000

function Write-Result($obj) {
    Write-Output ($obj | ConvertTo-Json -Depth 6 -Compress)
}

function Enable-DpiAwareness {
    $source = @'
using System;
using System.Runtime.InteropServices;
public static class LwNative {
    [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr value);
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern int GetSystemMetrics(int index);
}
'@
    Add-Type -TypeDefinition $source
    try {
        if ([LwNative]::SetProcessDpiAwarenessContext([IntPtr](-4))) { return }
    } catch { }
    try { [void][LwNative]::SetProcessDPIAware() } catch { }
}

function Resolve-Region {
    if ($W -le 0 -or $H -le 0) {
        return @{ x = 0; y = 0; w = [LwNative]::GetSystemMetrics(0); h = [LwNative]::GetSystemMetrics(1) }
    }
    return @{ x = $X; y = $Y; w = [Math]::Min($W, $MaxCaptureW); h = [Math]::Min($H, $MaxCaptureH) }
}

function New-Capture($region) {
    $bmp = New-Object System.Drawing.Bitmap($region.w, $region.h)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    try { $g.CopyFromScreen($region.x, $region.y, 0, 0, $bmp.Size) } finally { $g.Dispose() }
    return $bmp
}

function Resize-Bitmap($src, [double]$factor) {
    $newW = [Math]::Max(1, [int]($src.Width * $factor))
    $newH = [Math]::Max(1, [int]($src.Height * $factor))
    $dst = New-Object System.Drawing.Bitmap($newW, $newH)
    $g = [System.Drawing.Graphics]::FromImage($dst)
    try {
        $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
        $g.DrawImage($src, 0, 0, $newW, $newH)
    } finally { $g.Dispose() }
    return $dst
}

function Initialize-WinRt {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
    $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
    $script:AsTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })[0]
}

function Wait-WinRt($operation, [Type]$resultType) {
    $task = $script:AsTaskGeneric.MakeGenericMethod($resultType).Invoke($null, @($operation))
    if (-not $task.Wait($WinRtTimeoutMs)) { throw 'WinRT operation timed out' }
    return $task.Result
}

function Convert-OcrLines($ocr, $region, [int]$factor) {
    foreach ($line in $ocr.Lines) {
        $words = @($line.Words)
        if ($words.Count -eq 0) { continue }
        $left = [double]::MaxValue
        $top = [double]::MaxValue
        $right = 0.0
        $bottom = 0.0
        foreach ($word in $words) {
            $r = $word.BoundingRect
            $left = [Math]::Min($left, [double]$r.X)
            $top = [Math]::Min($top, [double]$r.Y)
            $right = [Math]::Max($right, [double]$r.X + [double]$r.Width)
            $bottom = [Math]::Max($bottom, [double]$r.Y + [double]$r.Height)
        }
        [ordered]@{
            text = $line.Text
            x = [int][Math]::Round($left / $factor) + $region.x
            y = [int][Math]::Round($top / $factor) + $region.y
            w = [int][Math]::Ceiling(($right - $left) / $factor)
            h = [int][Math]::Ceiling(($bottom - $top) / $factor)
            words = $words.Count
        }
    }
}

function Invoke-Ocr($bitmap, $region, [int]$factor) {
    $png = Join-Path ([System.IO.Path]::GetTempPath()) ('localwhisper_ocr_' + [Guid]::NewGuid().ToString('N') + '.png')
    $bitmap.Save($png, [System.Drawing.Imaging.ImageFormat]::Png)
    try {
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
        if ($null -eq $engine) { return @{ ok = $false; error = 'no_ocr_language_installed' } }
        $file = Wait-WinRt ([Windows.Storage.StorageFile]::GetFileFromPathAsync($png)) ([Windows.Storage.StorageFile])
        $stream = Wait-WinRt ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        try {
            $decoder = Wait-WinRt ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
            $soft = Wait-WinRt ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
            $ocr = Wait-WinRt ($engine.RecognizeAsync($soft)) ([Windows.Media.Ocr.OcrResult])
        } finally { $stream.Dispose() }
        return @{ ok = $true; scale = $factor; origin = $region; lines = @(Convert-OcrLines $ocr $region $factor) }
    } finally {
        Remove-Item -LiteralPath $png -ErrorAction SilentlyContinue
    }
}

function Invoke-OcrMode($region) {
    Initialize-WinRt
    $factor = [Math]::Max(1, [Math]::Min($Scale, 3))
    $maxDim = [Windows.Media.Ocr.OcrEngine]::MaxImageDimension
    if (($region.w * $factor) -gt $maxDim -or ($region.h * $factor) -gt $maxDim) { $factor = 1 }
    $capture = New-Capture $region
    try {
        $source = $capture
        if ($factor -gt 1) { $source = Resize-Bitmap $capture $factor }
        try { return Invoke-Ocr $source $region $factor }
        finally { if ($factor -gt 1) { $source.Dispose() } }
    } finally { $capture.Dispose() }
}

function Add-TargetMarker($bitmap, $region) {
    $g = [System.Drawing.Graphics]::FromImage($bitmap)
    try {
        $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
        $pen = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(255, 242, 61, 92), 3)
        try {
            if ($BoxW -gt 0 -and $BoxH -gt 0) {
                $g.DrawRectangle($pen, ($BoxX - $region.x - 3), ($BoxY - $region.y - 3), ($BoxW + 6), ($BoxH + 6))
            }
            if ($PointX -gt -100000 -and $PointY -gt -100000) {
                $px = $PointX - $region.x
                $py = $PointY - $region.y
                $g.DrawEllipse($pen, ($px - 10), ($py - 10), 20, 20)
            }
        } finally { $pen.Dispose() }
    } finally { $g.Dispose() }
}

function Save-Jpeg($bitmap, [string]$path, [long]$quality) {
    $codec = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() | Where-Object { $_.MimeType -eq 'image/jpeg' } | Select-Object -First 1
    $params = New-Object System.Drawing.Imaging.EncoderParameters(1)
    try {
        $params.Param[0] = New-Object System.Drawing.Imaging.EncoderParameter([System.Drawing.Imaging.Encoder]::Quality, $quality)
        $bitmap.Save($path, $codec, $params)
    } finally { $params.Dispose() }
}

function Save-ResizedJpeg($bitmap, [string]$path, [int]$maxWidth, [long]$quality) {
    $safeQuality = [Math]::Max(20, [Math]::Min($quality, 95))
    $factor = 1.0
    if ($maxWidth -gt 0 -and $bitmap.Width -gt $maxWidth) {
        $factor = $maxWidth / [double]$bitmap.Width
    }
    $output = $bitmap
    if ($factor -lt 1.0) { $output = Resize-Bitmap $bitmap $factor }
    $outW = $output.Width
    $outH = $output.Height
    try { Save-Jpeg $output $path $safeQuality }
    finally { if ($factor -lt 1.0) { $output.Dispose() } }
    return @{ ok = $true; width = $outW; height = $outH; source_width = $bitmap.Width; source_height = $bitmap.Height }
}

function Invoke-PreviewMode($region) {
    if ([string]::IsNullOrWhiteSpace($OutPath)) { throw 'OutPath is required for preview mode' }
    $capture = New-Capture $region
    try {
        Add-TargetMarker $capture $region
        return Save-ResizedJpeg $capture $OutPath $MaxWidth 84
    } finally { $capture.Dispose() }
}

function Invoke-ScreenshotMode($region) {
    if ([string]::IsNullOrWhiteSpace($OutPath)) { throw 'OutPath is required for screenshot mode' }
    $capture = New-Capture $region
    try {
        return Save-ResizedJpeg $capture $OutPath $MaxWidth $Quality
    } finally { $capture.Dispose() }
}

function Invoke-LoadImageMode {
    if ([string]::IsNullOrWhiteSpace($InPath)) { throw 'InPath is required for load_image mode' }
    if ([string]::IsNullOrWhiteSpace($OutPath)) { throw 'OutPath is required for load_image mode' }
    if (-not (Test-Path -LiteralPath $InPath -PathType Leaf)) { throw 'Image file does not exist' }
    $img = [System.Drawing.Image]::FromFile($InPath)
    try {
        $bmp = New-Object System.Drawing.Bitmap($img)
        try {
            return Save-ResizedJpeg $bmp $OutPath $MaxWidth $Quality
        } finally { $bmp.Dispose() }
    } finally { $img.Dispose() }
}

try {
    Enable-DpiAwareness
    Add-Type -AssemblyName System.Drawing
    if ($Mode -eq 'load_image') {
        Write-Result (Invoke-LoadImageMode)
    } else {
        $region = Resolve-Region
        if ($region.w -le 0 -or $region.h -le 0) { throw 'Empty capture region' }
        if ($Mode -eq 'ocr') {
            Write-Result (Invoke-OcrMode $region)
        } elseif ($Mode -eq 'preview') {
            Write-Result (Invoke-PreviewMode $region)
        } elseif ($Mode -eq 'screenshot') {
            Write-Result (Invoke-ScreenshotMode $region)
        } else {
            Write-Result @{ ok = $false; error = ('unknown mode: ' + $Mode) }
        }
    }
} catch {
    Write-Result @{ ok = $false; error = $_.Exception.Message }
}
