import argparse
import socket
from PIL import Image
#from IPython.display import display
import torch as th
import numpy as np
import json
from imagen_pytorch.model_creation import create_model_and_diffusion as create_model_and_diffusion_dalle2
from imagen_pytorch.model_creation import model_and_diffusion_defaults as model_and_diffusion_defaults_dalle2
from transformers import AutoTokenizer
import cv2
import glob
import os
from basicsr.archs.rrdbnet_arch import RRDBNet
from realesrgan import RealESRGANer
from realesrgan.archs.srvgg_arch import SRVGGNetCompact
from gfpgan import GFPGANer

class RESRGAN:
    def __init__(self, batch_size = 4):
        has_cuda = th.cuda.is_available()
        self.has_cuda = has_cuda
        device = th.device('cpu' if not has_cuda else 'cuda')
        self.device = device
        self.basedir = os.getenv('HOME')
        self.batch_size = batch_size
        self.options = model_and_diffusion_defaults_dalle2()
        self.options['use_fp16'] = False
        self.options['diffusion_steps'] = 200
        self.options['num_res_blocks'] = 3
        self.options['t5_name'] = 't5-3b'
        self.options['cache_text_emb'] = True

    def get_ready(self):
        print('getting RealESRGAN ready')
        model, diffusion = create_model_and_diffusion_dalle2(**self.options)
        self.model = model
        self.diffusion = diffusion
        model.eval()
        #if has_cuda:
        #    model.convert_to_fp16()
        model.to(self.device)
        #model.load_state_dict(_fix_path('/content/ImagenT5-3B/model.pt'))
        fn = self.basedir +  '/ImagenT5-3B/model.pt'
        print("fn",fn)
        model.load_state_dict(self._fix_path(fn)) 
        #print('total base parameters', sum(x.numel() for x in model.parameters()))
        num_params = sum(param.numel() for param in model.parameters())
        self.num_params = num_params
        #print('num_params', num_params)
        realesrgan_model = RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64,
                                   num_block=23, num_grow_ch=32, scale=4)
        self.realesrgan_model = realesrgan_model
        self.netscale = 4
        fn = self.basedir + '/Real-ESRGAN/experiments/pretrained_models/RealESRGAN_x4plus.pth'
        upsampler = RealESRGANer(
            scale=self.netscale,
            model_path=fn,
            model=realesrgan_model,
            tile=0,
            tile_pad=10,
            pre_pad=0,
            half=True
        )
        self.upsampler = upsampler
        face_enhancer = GFPGANer(
            model_path='https://github.com/TencentARC/GFPGAN/releases/download/v1.3.0/GFPGANv1.3.pth',
            upscale=4,
            arch='clean',
            channel_multiplier=2,
            bg_upsampler=upsampler
        )
        self.face_enhancer = face_enhancer
        tokenizer = AutoTokenizer.from_pretrained(self.options['t5_name'])
        self.tokenizer = tokenizer
        #print('uncond text encoding tokenizer')
        self.uncond_text_encoding = self.tokenizer(
            '',
            max_length=128,
            padding="max_length",
            truncation=True,
            return_attention_mask=True,
            add_special_tokens=True,
            return_tensors="pt"
        )

    def model_fn(self, x_t, ts, **kwargs):
        guidance_scale = 5
        half = x_t[: len(x_t) // 2]
        combined = th.cat([half, half], dim=0)
        model_out = self.model(combined, ts, **kwargs)
        eps, rest = model_out[:, :3], model_out[:, 3:]
        cond_eps, uncond_eps = th.split(eps, len(eps) // 2, dim=0)
        half_eps = uncond_eps + guidance_scale * (cond_eps - uncond_eps)
        eps = th.cat([half_eps, half_eps], dim=0)
        return th.cat([eps, rest], dim=1)

    #def show_images(batch: th.Tensor):
    #    """ Display a batch of images inline."""
    #    scaled = ((batch + 1)*127.5).round().clamp(0,255).to(th.uint8).cpu()
    #    reshaped = scaled.permute(2, 0, 3, 1).reshape([batch.shape[2], -1, 3])
    #    #display(Image.fromarray(reshaped.numpy()))
    #    im = Image.fromarray(reshaped.numpy())
    #    #im.save("test-000.jpg")

    def get_numpy_img(self, img):
        scaled = ((img + 1)*127.5).round().clamp(0,255).to(th.uint8).cpu()
        reshaped = scaled.permute(2, 0, 3, 1).reshape([img.shape[2], -1, 3])
        return cv2.cvtColor(reshaped.numpy(), cv2.COLOR_BGR2RGB)

    def _fix_path(self, path):
      d = th.load(path)
      checkpoint = {}
      for key in d.keys():
        checkpoint[key.replace('module.','')] = d[key]
      return checkpoint

    def gen_images(self, prompt, filename_prefix ):
        #@title What do you want to generate?
        #prompt = 'A photo of cat'#@param {type:"string"}
        #prompt = 'A unicorn rainbow dog on clouds'#@param {type:"string"}
        #print('text encoding tokenizer')
        print(f"gen_images: {prompt} {filename_prefix}")
        if prompt == '' or filename_prefix == '':
            print('Error: Missing prompt or filename prefix')
            return
        text_encoding = self.tokenizer(
            prompt,
            max_length=128,
            padding="max_length",
            truncation=True,
            return_attention_mask=True,
            add_special_tokens=True,
            return_tensors="pt"
        )
        uncond_text_encoding = self.uncond_text_encoding
        #print('numpy token attention mask')
        cond_tokens = th.from_numpy(np.array([text_encoding['input_ids'][0].numpy() for i in range(self.batch_size)]))
        uncond_tokens = th.from_numpy(np.array([uncond_text_encoding['input_ids'][0].numpy() for i in range(self.batch_size)]))
        cond_attention_mask = th.from_numpy(np.array([text_encoding['attention_mask'][0].numpy() for i in range(self.batch_size)]))
        uncond_attention_mask = th.from_numpy(np.array([uncond_text_encoding['attention_mask'][0].numpy() for i in range(self.batch_size)]))
        model_kwargs = {}
        model_kwargs["tokens"] = th.cat((cond_tokens,
                                         uncond_tokens)).to(self.device)
        model_kwargs["mask"] = th.cat((cond_attention_mask,
                                       uncond_attention_mask)).to(self.device)
        self.model.del_cache()
        #print('diffusion sample loop')
        sample = self.diffusion.p_sample_loop(
            self.model_fn,
            (self.batch_size * 2, 3, 64, 64),
            clip_denoised=True,
            model_kwargs=model_kwargs,
            device='cuda',
            progress=True,
        )[:self.batch_size]
        self.model.del_cache()
        #print('show images')
        #show_images(sample)
        #print('unsqueeze')
        #for i in sample:
        #    show_images(i.unsqueeze(0))
        #print('get numpy img')
        new_img = self.get_numpy_img(sample)
        for j in range(self.batch_size):
            new_img = self.get_numpy_img(sample[j].unsqueeze(0))
            for i in range(1):
                #print('face enhancer', j)
                _, _, new_img = self.face_enhancer.enhance(new_img, has_aligned=False,
                                                      only_center_face=False, paste_back=True)
                imagefile_name = filename_prefix + str(j) + '.jpg'
                cv2.imwrite(imagefile_name, new_img)
                print('save image ', imagefile_name)
                # img = Image.open(imagefile_name)
                # w, h = img.size
                # newsize = (w * 2, h* 2)
                # newimg = img.resize(newsize)
                # newimg.save(imagefile_name)
                # from google.cloud import storage as gcs
                # gcs_client = gcs.Client()
                # bucket_name = 'mybucket'
                # bucket = gcs_client.get_bucket(bucket_name)
                # blob = gcs.Blob('blobxyz', bucket)
                # blob.upload_from_filename(imagefile_name)

def test1():
    reg = RESRGAN()
    reg.get_ready()
    prompts = [
            { "filename": "dogcatunicorn", "story": "a fancy dog and a funny cat on a cloud and a rainbow frolicing with a unicorn" },
            { "filename": "elephantgiraffe", "story": "elephants and giraffes playing on a swing hanging from the rainbow clouds"},
            { "filename": "pandapolar", "story": "panda bears and polar bears racing on skis made of bamboo and ice"}
        ]
    for prompt in prompts:
        reg.gen_images( prompt["story"] , prompt["filename"] )
    
def run_server(port=5000, max_length=1440):
    reg = RESRGAN()
    reg.get_ready()
    server_socket = socket.socket()
    server_socket.bind(('0.0.0.0', port))
    server_socket.listen(5)
    print("server socket litening on port ",port)
    while True:
        print("accepting connection")
        conn, address = server_socket.accept()
        print("connection from " + str(address))
        while True:
            data = conn.recv(max_length).decode()
            if not data:
                break
            print(f"got {data}")
            json_data = json.loads(data)
            story_prompt = json_data["story_prompt"]
            filename = json_data["filename"]
            reg.gen_images(story_prompt, filename)
            conn.send("done".encode())

    print("server exiting")
    conn.close()

if __name__ == '__main__':
    #test1()
    parser = argparse.ArgumentParser(description="realESRGAN socket server")
    #parser.add_argument("-p","--port",type=int,default=5000,help="port",required=True)
    parser.add_argument("-p","--port",type=int,default=5000,help="port")
    parser.add_argument("-l","--max_length",type=int,default=1440,help="maximum number of bytes to read and write per message")
    args = vars(parser.parse_args())
    run_server(args["port"],args["max_length"])
    print('exiting program')
