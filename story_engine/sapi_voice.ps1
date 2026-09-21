param([Parameter(Mandatory=$true)][string]$Manifest)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$storyVoice = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $englishVoice = $storyVoice.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.Name -like 'en-*' } | Select-Object -First 1
    if (-not $englishVoice) { throw 'An English development voice is required.' }
    $storyVoice.SelectVoice($englishVoice.VoiceInfo.Name)
    $storyVoice.Rate = 2
    $storyVoice.Volume = 100
    $segments = Get-Content -Raw -LiteralPath $Manifest -Encoding UTF8 | ConvertFrom-Json
    foreach ($segment in $segments) {
        if ($null -ne $segment.voice_index) {
            $dialogueVoices = @($storyVoice.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.Name -like 'en-*' })
            $storyVoice.SelectVoice($dialogueVoices[[int]$segment.voice_index % $dialogueVoices.Count].VoiceInfo.Name)
        }
        if ($null -ne $segment.rate) { $storyVoice.Rate = [int]$segment.rate }
        $format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(22050, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
        $storyVoice.SetOutputToWaveFile($segment.path, $format)
        $storyVoice.Speak([string]$segment.text)
        $storyVoice.SetOutputToNull()
    }
} finally {
    $storyVoice.Dispose()
}
