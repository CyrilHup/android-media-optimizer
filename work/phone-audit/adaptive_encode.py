"""Rate-controlled HEVC options and bounded, real encoder capability checks."""
import subprocess


def encoder_options(encoder, bitrate, maximum, pixel_format='yuv420p', dolby=False):
    if not 0 < bitrate <= maximum:
        raise ValueError('Video bitrate must fit the file budget')
    # A bounded peak also constrains short samples and avoids huge CQP outputs.
    peak = min(maximum, int(bitrate * 1.35))
    rate = ['-b:v', str(bitrate), '-maxrate', str(peak), '-bufsize', str(peak * 2)]
    if encoder == 'nvenc':
        if dolby:
            raise ValueError('Dolby metadata preservation requires the validated x265 route')
        return ['-c:v', 'hevc_nvenc', '-preset', 'p4', '-rc', 'vbr', '-multipass', 'disabled', *rate, '-pix_fmt', 'p010le' if pixel_format == 'yuv420p10le' else pixel_format, '-g', '60']
    if encoder == 'amf':
        if dolby:
            raise ValueError('Dolby metadata preservation requires the validated x265 route')
        return ['-c:v', 'hevc_amf', '-quality', 'balanced', '-rc', 'vbr_peak', *rate, '-pix_fmt', 'p010le' if pixel_format == 'yuv420p10le' else pixel_format, '-g', '60']
    if encoder != 'x265':
        raise ValueError('Unknown encoder: ' + encoder)
    params = 'pools=6:frame-threads=2'
    if dolby:
        params += ':dolby-vision-profile=8.4:colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc:range=limited'
    return ['-c:v', 'libx265', '-preset', 'veryfast', *rate, '-x265-params', params, '-pix_fmt', pixel_format, '-g', '60', *(['-dolbyvision', '1', '-strict', 'unofficial'] if dolby else [])]


def detect_encoders(ffmpeg, log_folder):
    """Probe actual execution, not just compile-time encoder enumeration."""
    available = []
    results = {}
    for encoder in ('nvenc', 'amf'):
        args = [ffmpeg, '-v', 'error', '-nostdin', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=30', '-frames:v', '3', *encoder_options(encoder, 300000, 600000), '-f', 'null', '-']
        try:
            result = subprocess.run(args, capture_output=True, timeout=30)
            error = result.stderr.decode(errors='replace')
            success = result.returncode == 0
        except subprocess.TimeoutExpired:
            error, success = 'Encoder capability probe timed out', False
        (log_folder / (encoder + '-capability.log')).write_text(error, encoding='utf-8')
        results[encoder] = {'available': success, 'error': error[-2000:] if not success else None}
        if success:
            available.append(encoder)
    return available, results
