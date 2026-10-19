"""SDR perceptual screen; HDR uses a common display transform plus raw metrics."""
HDR_DISPLAY='zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p'

def quality_filter(width,height,pixel_range,name,hdr=False):
    inputs=f'[0:v]select=not(mod(n\\,3)),settb=AVTB,setpts=N/(10*TB),split=3[dv0][dp][ds];[1:v]scale={width}:{height}:flags=lanczos:in_range={pixel_range}:out_range={pixel_range},select=not(mod(n\\,3)),settb=AVTB,setpts=N/(10*TB),split=3[rv0][rp][rs];'
    display=f'[dv0]{HDR_DISPLAY}[dv];[rv0]{HDR_DISPLAY}[rv];' if hdr else '[dv0]null[dv];[rv0]null[rv];'
    return inputs+display+f'[dv][rv]libvmaf=model=version=vmaf_v0.6.1:log_fmt=json:log_path={name}.json:n_threads=4[v];[dp][rp]psnr[p];[ds][rs]ssim[s]'
