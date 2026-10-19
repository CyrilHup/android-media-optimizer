"""Format-aware encoder policy; unsuccessful encodes never replace originals."""
VERSION=6
COLOR_ENUMS={'bt709':1,'bt470bg':5,'smpte170m':6,'bt2020':9,'bt2020nc':9,'bt2020c':10,'smpte2084':16,'arib-std-b67':18}

def receipt_is_final(receipt):
    status=receipt.get('status','')
    return status=='completed' or (status.startswith('skipped') and receipt.get('policy_version',1)>=VERSION)

def classify(video,duration):
    if video.get('codec_name') not in ('h264','hevc') or duration<5:
        return None,'unsupported_codec_or_very_short'
    if video.get('pix_fmt') not in ('yuv420p','yuvj420p','yuv420p10le'):
        return None,'unsupported_pixel_format'
    if video.get('color_range') not in ('tv','pc') or any(video.get(k) not in COLOR_ENUMS for k in ('color_space','color_transfer','color_primaries')):
        return None,'unknown_colour_metadata'
    hdr=video['color_transfer'] in ('smpte2084','arib-std-b67')
    dolby=any(d.get('side_data_type')=='DOVI configuration record' for d in video.get('side_data_list',[]))
    if dolby:
        config=next(d for d in video['side_data_list'] if d.get('side_data_type')=='DOVI configuration record')
        supported=config.get('dv_profile')==8 and config.get('dv_bl_signal_compatibility_id')==4 and config.get('rpu_present_flag')==1 and config.get('el_present_flag')==0 and config.get('bl_present_flag')==1 and video['pix_fmt']=='yuv420p10le' and video['color_range']=='tv' and video['color_space']=='bt2020nc' and video['color_transfer']=='arib-std-b67' and video['color_primaries']=='bt2020'
        if not supported:
            return None,'unsupported_dolby_vision_profile'
    if hdr and not dolby:
        return None,'hdr_preservation_not_yet_validated'
    return {'hdr':hdr,'dolby':dolby,'pixel_format':'yuv420p10le' if video['pix_fmt']=='yuv420p10le' else 'yuv420p'},None

def quality_passes(sample,hdr=False):
    # User explicitly accepts a broader visually-close tolerance. HDR still
    # needs the native-pixel screen as well as the perceptual screen.
    perceptual=sample['vmaf_mean']>=90 and sample['vmaf_p5']>=80
    psnr=sample['psnr_average_db']
    pixels=((psnr is None and sample.get('psnr_infinite') is True) or (psnr is not None and psnr>=38)) and sample['ssim_all']>=.97
    return perceptual and pixels if hdr else perceptual or pixels

def failure_reason(attempts):
    """Report the actual combination of outcomes instead of any early failure."""
    if any(t.get('result')=='quality_screen_failed' and not t.get('sample_preflight_only') for t in attempts):
        return 'full_encode_quality_mismatch'
    quality=any(t.get('result')=='quality_screen_failed' for t in attempts)
    size=any(t.get('result') in ('sample_size_budget_exceeded','less_than_10_percent_saving') for t in attempts)
    if quality and size:
        return 'no_size_quality_combination'
    if quality:
        return 'quality_screen_after_all_encoders'
    if size:
        return 'less_than_10_percent_saving'
    return 'encoder_error_after_all_encoders'
