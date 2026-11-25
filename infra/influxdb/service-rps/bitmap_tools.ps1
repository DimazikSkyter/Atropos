param(
    [ValidateSet('extract', 'preview')][string]$Mode,
    [string]$InputPath,
    [string]$OutputPath
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
if ($Mode -eq 'extract') {
    $bitmap = [System.Drawing.Bitmap]::new($InputPath)
    try {
        $rect = [System.Drawing.Rectangle]::new(0, 0, $bitmap.Width, $bitmap.Height)
        $data = $bitmap.LockBits($rect, [System.Drawing.Imaging.ImageLockMode]::ReadOnly, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
        try {
            $bytes = [byte[]]::new($data.Stride * $bitmap.Height)
            [System.Runtime.InteropServices.Marshal]::Copy($data.Scan0, $bytes, 0, $bytes.Length)
            [System.IO.File]::WriteAllBytes($OutputPath, $bytes)
            @{width=$bitmap.Width; height=$bitmap.Height; stride=$data.Stride} | ConvertTo-Json -Compress
        } finally { $bitmap.UnlockBits($data) }
    } finally { $bitmap.Dispose() }
    exit
}

$rows = Import-Csv -LiteralPath $InputPath
$bitmap = [System.Drawing.Bitmap]::new(1800, 850)
$g = [System.Drawing.Graphics]::FromImage($bitmap)
$g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
$g.Clear([System.Drawing.ColorTranslator]::FromHtml('#181B1F'))
$font = [System.Drawing.Font]::new('Arial', 14)
$titleFont = [System.Drawing.Font]::new('Arial', 20)
$white = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(215, 219, 225))
$grid = [System.Drawing.Pen]::new([System.Drawing.Color]::FromArgb(50, 54, 61), 1)
$left = 115; $right = 1755; $top = 75; $bottom = 735
$start = [DateTimeOffset]::Parse('2026-10-04T20:27:00+03:00')
$end = [DateTimeOffset]::Parse('2026-10-04T23:27:00+03:00')
for ($value = 0; $value -le 2000; $value += 500) {
    $y = $bottom - $value / 2200.0 * ($bottom - $top)
    $g.DrawLine($grid, [single]$left, [single]$y, [single]$right, [single]$y)
    $g.DrawString([string]$value, $font, $white, 35, [single]($y - 13))
}
for ($minute = 30; $minute -le 180; $minute += 30) {
    $time = [DateTimeOffset]::Parse('2026-10-04T20:00:00+03:00').AddMinutes($minute)
    $x = $left + ($time - $start).TotalSeconds / ($end - $start).TotalSeconds * ($right - $left)
    $g.DrawLine($grid, [single]$x, [single]$top, [single]$x, [single]$bottom)
    $g.DrawString($time.ToString('HH:mm'), $font, $white, [single]($x - 26), 750)
}
$palette = @{'service1'='#73BF69'; 'service2'='#F2CC0C'; 'service3'='#8AB8FF'; 'service4'='#FF780A'; 'service5'='#F2495C'}
$index = 0
foreach ($service in @('service1', 'service2', 'service3', 'service4', 'service5')) {
    $pen = [System.Drawing.Pen]::new([System.Drawing.ColorTranslator]::FromHtml($palette[$service]), 2)
    $points = [System.Collections.Generic.List[System.Drawing.PointF]]::new()
    foreach ($row in ($rows | Where-Object service -eq $service)) {
        $time = [DateTimeOffset]::Parse($row.timestamp)
        $value = [double]::Parse($row.rps, [System.Globalization.CultureInfo]::InvariantCulture)
        $x = $left + ($time - $start).TotalSeconds / ($end - $start).TotalSeconds * ($right - $left)
        $y = $bottom - $value / 2200.0 * ($bottom - $top)
        $points.Add([System.Drawing.PointF]::new([single]$x, [single]$y))
    }
    $g.DrawLines($pen, $points.ToArray())
    $lx = 160 + $index * 265
    $g.DrawLine($pen, $lx, 806, ($lx + 40), 806)
    $g.DrawString($service, $font, $white, ($lx + 49), 793)
    $pen.Dispose()
    $index++
}
$g.DrawString('Service RPS | 04 October 2026 | Europe/Moscow', $titleFont, $white, 115, 20)
$bitmap.Save($OutputPath, [System.Drawing.Imaging.ImageFormat]::Png)
$grid.Dispose(); $white.Dispose(); $font.Dispose(); $titleFont.Dispose(); $g.Dispose(); $bitmap.Dispose()
