"""Focused numeric guards; never imports or runs the media worker."""
from copy import deepcopy
import unittest

from compression_workflow import cpu_only_row, valid_preflight


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.identity={'original_sha256':'verified-source','encode_args':['same-recipe']}
        self.starts=[1,5,9]
        self.cache={'identity':deepcopy(self.identity),'sample_sizes':[100,110,120],'sample_encode_seconds':[2,2,2],
                    'quality_windows':[{'start_seconds':start,'sampled_frames':90,'vmaf_mean':92,'vmaf_p5':84,'psnr_average_db':39,'ssim_all':.98} for start in self.starts]}

    def test_reuse_requires_exact_source_and_recipe(self):
        self.assertTrue(valid_preflight(self.cache,self.identity,self.starts,True))
        for key,value in [('original_sha256','changed'),('encode_args',['changed'])]:
            changed={**self.identity,key:value}
            self.assertFalse(valid_preflight(self.cache,changed,self.starts,False))

    def test_incomplete_wrong_sections_and_invalid_sizes_are_rejected(self):
        for key,value in [('quality_windows',self.cache['quality_windows'][:2]),('sample_sizes',[1,0,2]),('sample_encode_seconds',[1,float('nan'),2])]:
            self.assertFalse(valid_preflight({**self.cache,key:value},self.identity,self.starts,False))
        self.assertFalse(valid_preflight(self.cache,self.identity,[2,5,9],False))

    def test_quality_is_rechecked_in_current_hdr_domain(self):
        changed=deepcopy(self.cache)
        changed['quality_windows'][0].update(psnr_average_db=35,ssim_all=.95)
        self.assertTrue(valid_preflight(changed,self.identity,self.starts,False))
        self.assertFalse(valid_preflight(changed,self.identity,self.starts,True))

    def test_only_matching_supported_dolby_metadata_marks_cpu_only(self):
        row={'path':'A.mov','bytes':100,'mtime':2}
        video={'codec_type':'video','codec_name':'hevc','pix_fmt':'yuv420p10le','color_range':'tv','color_space':'bt2020nc','color_transfer':'arib-std-b67','color_primaries':'bt2020',
               'side_data_list':[{'side_data_type':'DOVI configuration record','dv_profile':8,'dv_bl_signal_compatibility_id':4,'rpu_present_flag':1,'el_present_flag':0,'bl_present_flag':1}]}
        cache={'A.mov':{'original_bytes':100,'mtime':2,'probe':{'streams':[video],'format':{'duration':'10'}}}}
        self.assertTrue(cpu_only_row(row,cache))
        self.assertFalse(cpu_only_row({**row,'mtime':3},cache))
        self.assertFalse(cpu_only_row(row,{}))
        video['side_data_list']=[]
        self.assertFalse(cpu_only_row(row,cache))


if __name__=='__main__':
    unittest.main()
