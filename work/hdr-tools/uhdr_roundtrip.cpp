// Local numeric-only Ultra HDR harness, using Google's reference API.
#include "ultrahdr_api.h"
#include <fstream>
#include <vector>
#include <iostream>
#include <filesystem>
#include <cstring>
#include <stdexcept>
#include <cmath>
namespace fs=std::filesystem;
using Bytes=std::vector<unsigned char>;
Bytes read(const fs::path& p){std::ifstream f(p,std::ios::binary);if(!f)throw std::runtime_error("Input unavailable");return Bytes(std::istreambuf_iterator<char>(f),{});}
void write(const fs::path& p,const void* data,size_t size){std::ofstream f(p,std::ios::binary);f.write((const char*)data,size);if(!f)throw std::runtime_error("Output failed");}
void check(uhdr_error_info_t e){if(e.error_code!=UHDR_CODEC_OK)throw std::runtime_error(e.has_detail?e.detail:"Ultra HDR operation failed");}
uhdr_compressed_image_t image(Bytes& bytes){return {bytes.data(),bytes.size(),bytes.size(),UHDR_CG_UNSPECIFIED,UHDR_CT_UNSPECIFIED,UHDR_CR_UNSPECIFIED};}
struct Decoder{
 Bytes bytes;uhdr_compressed_image_t img;uhdr_codec_private_t* ctx;
 Decoder(const fs::path& p):bytes(read(p)),img(image(bytes)),ctx(uhdr_create_decoder()){
  if(!ctx)throw std::runtime_error("Decoder unavailable");check(uhdr_dec_set_image(ctx,&img));check(uhdr_dec_set_out_img_format(ctx,UHDR_IMG_FMT_64bppRGBAHalfFloat));check(uhdr_dec_set_out_color_transfer(ctx,UHDR_CT_LINEAR));check(uhdr_dec_probe(ctx));
 }
 ~Decoder(){uhdr_release_decoder(ctx);}
 uhdr_raw_image_t* decode(){check(uhdr_decode(ctx));return uhdr_get_decoded_image(ctx);}
};
bool same(uhdr_mem_block_t* a,uhdr_mem_block_t* b){return a&&b&&a->data_sz==b->data_sz&&(!a->data_sz||!std::memcmp(a->data,b->data,a->data_sz));}
int main(int argc,char** argv){try{
 if(argc<4)throw std::runtime_error("extract source directory | pack reference base output | compare source target");
 std::string mode=argv[1];Decoder original(argv[2]);
 if(mode=="extract"){
  fs::create_directories(argv[3]);auto base=uhdr_dec_get_base_image(original.ctx);auto gain=uhdr_dec_get_gainmap_image(original.ctx);
  if(!base||!gain)throw std::runtime_error("Missing base or gain map");
  write(fs::path(argv[3])/"base.jpg",base->data,base->data_sz);write(fs::path(argv[3])/"gainmap.jpg",gain->data,gain->data_sz);
  std::cout<<"{\"base_bytes\":"<<base->data_sz<<",\"gainmap_bytes\":"<<gain->data_sz<<",\"width\":"<<uhdr_dec_get_image_width(original.ctx)<<",\"height\":"<<uhdr_dec_get_image_height(original.ctx)<<"}\n";
 }else if(mode=="pack"){
  if(argc!=5)throw std::runtime_error("pack requires output");
  auto baseBytes=read(argv[3]);auto base=image(baseBytes);auto gain=uhdr_dec_get_gainmap_image(original.ctx);
  uhdr_compressed_image_t gainImg={gain->data,gain->data_sz,gain->data_sz,UHDR_CG_UNSPECIFIED,UHDR_CT_UNSPECIFIED,UHDR_CR_UNSPECIFIED};
  auto encoder=uhdr_create_encoder();if(!encoder)throw std::runtime_error("Encoder unavailable");
  check(uhdr_enc_set_compressed_image(encoder,&base,UHDR_BASE_IMG));
  check(uhdr_enc_set_gainmap_image(encoder,&gainImg,uhdr_dec_get_gainmap_metadata(original.ctx)));
  check(uhdr_encode(encoder));auto output=uhdr_get_encoded_stream(encoder);write(argv[4],output->data,output->data_sz);uhdr_release_encoder(encoder);
  std::cout<<"{\"packaged\":true}\n";
 }else if(mode=="compare"){
  Decoder target(argv[3]);
  auto a=original.decode();auto b=target.decode();
  bool pixels=a&&b&&a->w==b->w&&a->h==b->h&&a->fmt==b->fmt;
  if(pixels)for(unsigned y=0;y<a->h;y++)if(std::memcmp((char*)a->planes[0]+y*a->stride[0]*8,(char*)b->planes[0]+y*b->stride[0]*8,a->w*8)){pixels=false;break;}
  auto ma=uhdr_dec_get_gainmap_metadata(original.ctx);auto mb=uhdr_dec_get_gainmap_metadata(target.ctx);
  float error=0;for(int c=0;c<3;c++){
   error=std::max(error,std::fabs(ma->max_content_boost[c]-mb->max_content_boost[c]));
   error=std::max(error,std::fabs(ma->min_content_boost[c]-mb->min_content_boost[c]));
   error=std::max(error,std::fabs(ma->gamma[c]-mb->gamma[c]));
   error=std::max(error,std::fabs(ma->offset_sdr[c]-mb->offset_sdr[c]));
   error=std::max(error,std::fabs(ma->offset_hdr[c]-mb->offset_hdr[c]));
  }
  error=std::max(error,std::fabs(ma->hdr_capacity_min-mb->hdr_capacity_min));error=std::max(error,std::fabs(ma->hdr_capacity_max-mb->hdr_capacity_max));
  std::cout<<std::boolalpha<<"{\"full_hdr_decode_passed\":true,\"linear_fp16_pixels_identical\":"<<pixels<<",\"exif_identical\":"<<same(uhdr_dec_get_exif(original.ctx),uhdr_dec_get_exif(target.ctx))<<",\"icc_identical\":"<<same(uhdr_dec_get_icc(original.ctx),uhdr_dec_get_icc(target.ctx))<<",\"max_gainmap_metadata_absolute_difference\":"<<error<<",\"gainmap_colour_space_flag_identical\":"<<(ma->use_base_cg==mb->use_base_cg)<<"}\n";
 }else throw std::runtime_error("Unknown mode");
 return 0;
}catch(const std::exception& e){std::cerr<<e.what()<<"\n";return 1;}}
