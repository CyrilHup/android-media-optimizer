"""MP4 finalization maps supported media and regenerates timecode from tags."""
def remux_args(ffmpeg,source,target,bitstream_filter,dolby=False):
    return [ffmpeg,'-hide_banner','-nostdin','-y','-i',str(source),'-map','0:v:0','-map','0:a?','-c','copy','-map_metadata','0','-bsf:v',bitstream_filter,'-tag:v','hvc1',*(['-strict','unofficial'] if dolby else []),'-movie_timescale','1000000','-movflags','+faststart+use_metadata_tags',str(target)]
