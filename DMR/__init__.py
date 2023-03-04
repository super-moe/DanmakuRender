import logging
import threading
import multiprocessing
import queue
import time
from .Uploader import Uploader
from .Render import Render
from .Downloader import Downloader
from .utils import Config

class DanmakuRender():
    def __init__(self, config:Config, debug=False) -> None:
        self.config = config
        self.debug = debug
        self.stoped = True

        self.downloaders = {}
        self.uploaders = {}
        self.signal_queue = queue.Queue()
        
    def start(self):
        self.stoped = False
        self.monitor = threading.Thread(target=self.start_monitor,daemon=True)
        self.monitor.start()

        self.render = Render(pipe=self.signal_queue, debug=self.debug, **self.config.render_config)
        self.render.start()

        if self.config.uploader_config:
            for name, uploader_conf in self.config.uploader_config.items():
                uploader = Uploader(name,self.signal_queue,uploader_conf,debug=True)
                proc = uploader.start()
                self.uploaders[name] = {
                    'class':uploader,
                    'proc':proc,
                    'config':uploader_conf
                }

        for taskname, replay_conf in self.config.replay_config.items():
            logging.getLogger().info(f'添加直播：{replay_conf["url"]}')
            
            downloader = Downloader(taskname=taskname, pipe=self.signal_queue, debug=self.debug, **replay_conf)
            proc = downloader.start()

            self.downloaders[taskname] = {
                'class': downloader,
                'proc': proc,
                'status': None,
            }

    def start_monitor(self):
        while not self.stoped:
            msg = self.signal_queue.get()
            logging.debug(f'PIPE MESSAGE: {msg}')
            if msg.get('src') == 'downloader':
                self.process_downloader_message(msg)
            elif msg.get('src') == 'render':
                self.process_render_message(msg)
            elif msg.get('src') == 'uploader':
                self.process_uploader_message(msg)

    def process_uploader_message(self,msg):
        type = msg['type']
        if type == 'info':
            fp = msg['msg']
            logging.info(f'分片 {fp} 上传完成.')
            logging.info(msg.get('desc'))
        elif type == 'error':
            fp = msg['msg']
            logging.error(f'分片 {fp} 上传错误.')
            logging.exception(msg.get('desc'))
    
    def _dist_to_uploader(self, _name, _type, _item, _group=None, _video_info=None, **kwargs):
        uploaders = self.config.get_replay_config(_name).get('upload')
        if uploaders and uploaders.get(_type):
            upds = uploaders.get(_type)
            for upd in upds:
                uploader = self.uploaders[upd]['class']
                upd_conf = self.uploaders[upd]['config']
                uploader.add(_item, group=(_group,_type), video_info=_video_info, **upd_conf, **kwargs)

    def process_downloader_message(self, msg):
        type = msg['type']
        group = msg['group']
        conf = self.config.get_replay_config(group)
        if type == 'info':
            info = msg['msg']
            if info == 'start':
                self.downloaders[group]['status'] = 'start'
                logging.info(f'{group} 录制开始.')
            elif info == 'end':
                if self.downloaders[group]['status'] is None:
                    logging.info(f'{group} 直播结束，正在等待.')
                elif self.downloaders[group]['status'] == 'start':
                    logging.info(f'{group} 录制结束，正在等待.')
                    if conf.get('danmaku') and conf.get('auto_render'):
                        self.render.add('end', group=group)
                    if conf.get('upload'):
                        self._dist_to_uploader(group, 'src_video', 'end', group)
                self.downloaders[group]['status'] = 'end'
        
        elif type == 'split':
            fp = msg['msg']
            logging.info(f'分片 {fp} 录制完成.')

            if conf.get('danmaku') and conf.get('auto_render'):
                logging.info(f'添加分片 {fp} 至渲染队列.')
                self.render.add(fp, group=group, video_info=msg['video_info'])
            
            if conf.get('upload'):
                self._dist_to_uploader(group, 'src_video', fp, group, msg.get('video_info'))

        elif type == 'error':
            logging.error(f'录制 {group} 遇到错误，即将重试.')
            logging.exception(msg.get('desc'))

    def process_render_message(self, msg):
        type = msg['type']
        group = msg['group']
        conf = self.config.get_replay_config(group)
        if type == 'info':
            fp = msg['msg']
            logging.info(f'分片 {fp} 渲染完成.')
            logging.info(msg.get('desc'))

            if conf.get('upload'):
                self._dist_to_uploader(group, 'dm_video', fp, group, msg.get('video_info'))

        elif type == 'end':
            logging.info(f'完成对 {group} 的全部视频渲染.')
            if conf.get('upload'):
                self._dist_to_uploader(group, 'dm_video', 'end', group)
            
        elif type == 'error':
            fp = msg['msg']
            logging.error(f'分片 {fp} 渲染错误.')
            logging.exception(msg.get('desc'))

    def stop(self):
        self.stoped = True
        for taskname, task in self.downloaders.items():
            try:
                task['class'].stop()
            except Exception as e:
                logging.exception(e)
                
        self.downloaders.clear()
        self.render.stop()
        time.sleep(1)
    
        