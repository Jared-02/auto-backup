import os
import subprocess
import logging
import configparser
import json
from datetime import datetime
from logging import handlers

from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive

class GoogleDriveBackup(object):

    def __init__(self):
        script_dir = os.path.dirname(os.path.realpath(__file__))
        conf = f"{script_dir}/gd_conf.ini"
        if not os.path.exists(conf):
            raise FileNotFoundError('Please place the configuration file in the same directory!')
        config = configparser.ConfigParser()
        config.read(conf, encoding='utf-8')

        settings = {
            "client_config_file": "client_secrets.json",
            "save_credentials": True,
            "save_credentials_backend": "file",
            "save_credentials_file": "credentials.json",
            "get_refresh_token": True
        }
        gauth = GoogleAuth(settings=settings)
        gauth.LocalWebserverAuth()
        self.drive = GoogleDrive(gauth)

        self.website_path = config.get('Global', 'website_path')
        self.store_backup_path = config.get('Global', 'store_backup_path')
        self.folder_name = config.get('Global', 'remote_backup_path')
        self.folder_id = config.get('Global', 'remote_backup_path_id')
        self.keep_local_backup = config.getboolean('Global', 'keep_local_backup')
        self.keep_history = int(config.get('Global', 'keep_history'))

        self.logger = self.__getlog__()

    def __getlog__(self):
        """
        初始化 logger
        """
        log_dir = f"{self.store_backup_path}/.log"
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        log_file = f"{log_dir}/run.log"

        formatter = logging.Formatter('%(asctime)s - %(filename)s[%(lineno)s] - %(levelname)s - %(message)s')
        time_handler = handlers.TimedRotatingFileHandler(filename=log_file, when="midnight", backupCount=30, encoding='utf-8')
        time_handler.suffix = "%Y%m%d"
        time_handler.setLevel(logging.DEBUG)
        time_handler.setFormatter(formatter)

        logger = logging.getLogger('gd_backup')
        logger.setLevel(logging.DEBUG)
        logger.addHandler(time_handler)

        return logger

    def remote_file_upload(self, remote_file_path: str, file_path: str):
        """
        上传接口
        """
        metadata = self.drive_file_meta(remote_file_path)
        remote_file = self.drive.CreateFile(metadata=metadata)
        remote_file.SetContentFile(file_path)
        remote_file.Upload()

    def remote_file_delete(self, remote_file_path: str):
        """
        删除接口
        """
        file = self.search_file_meta(remote_file_path)
        file.Trash()

    def drive_file_meta(self, file_path: str):
        file_title = file_path.split("/")[1]
        metadata = {
            'parents': [{'id': self.folder_id}],
            'title': file_title 
        }

        return metadata

    def search_file_meta(self, file_path: str):
        file_title = file_path.split("/")[1]
        query = {'q': f"'{self.folder_id}' in parents and title = '{file_title}'"}
        file_list = self.drive.ListFile(query).GetList()

        return file_list[0]

    def local_dir_exists(self, check_dir: str):
        """
        本地目录存在性检查，不存在则创建
        """
        if not os.path.exists(check_dir):
            os.makedirs(check_dir)


    def backup_website(self):
        """
        备份网站目录
        """
        websites = self.website_path.split(',')
        # 经检查存在的目录
        existing_websites = []
        for web in websites:
            if not os.path.exists(web):
                self.logger.warning(f"Website path: [{web}] does not exist, skipped")
                continue
            else:
                existing_websites.append(web)

        website_via_tar = []
        store_sites_backups = self.store_backup_path
        self.local_dir_exists(store_sites_backups)

        for source_dir in existing_websites:
            tar_dir = os.path.join(store_sites_backups,
                        f"{os.path.basename(source_dir)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.tar.gz")
            tar_command = f"tar -czf {tar_dir} {source_dir}"
            subprocess.run(tar_command, shell=True)
            self.logger.info(f"[{source_dir}] has been locally backed up to {tar_dir}")
            website_via_tar.append(tar_dir)

        return website_via_tar

    def backup_upload(self):
        """
        上传主逻辑：生成备份上传云端，释放本地备份
        """
        backup_files = self.backup_website()
        remote_files = []

        for i in backup_files:
            # i 本地存放的备份
            relative_path = os.path.relpath(i, self.store_backup_path)
            remote_path = f"{self.folder_name}/{relative_path}"
            self.remote_file_upload(remote_path, i)
            self.logger.info(f"Local backup [{i}] has been uploaded to Google Drive: [{remote_path}]")

            remote_files.append(remote_path)
            if self.keep_local_backup == False:
                os.remove(i)
                self.logger.info(f"Free up local backup [{i}]")

        return (backup_files, remote_files)


    def write_backup_history(self, history: dict, local_backup: list, remote_backup: list):
        """
        写入备份历史
        """
        record_time = datetime.now()
        record_timestamp = int(datetime.timestamp(record_time))
        record_isotime = record_time.astimezone().isoformat(timespec='seconds')
        record = {
            record_timestamp: {
                'time': record_isotime,
                'localBackup': local_backup if self.keep_local_backup else None,
                'remoteBackup': remote_backup,
            }
        }
        history.update(record)


    def remove_backup_history(self, backup_history: dict):
        """
        移除过时的备份历史
        """
        records_time = [i for i in backup_history.keys()]
        records_time.sort(reverse=True)
        max_keep = self.keep_history - 1
        remove_records_time = records_time[max_keep:]

        for i in remove_records_time:
            local_backup = backup_history[i]['localBackup']
            remote_backup = backup_history[i]['remoteBackup']
            # 移除本地
            if local_backup is not None:
                for j in local_backup:
                    try:
                        os.remove(j)
                        self.logger.info(f"Free up expired local backup [{j}]")
                    except FileNotFoundError:
                        self.logger.warning(f"Local backup [{j}] has been removed.")
            # 移除云端
            for j in remote_backup:
                self.remote_file_delete(j)
                self.logger.info(f"Free up expired remote backup {j}")
                del backup_history[i]

    def manage_backup_history(self, local_backup: list, remote_backup: list):
        """
        管理备份历史：查、删、增
        """
        write_path = f"{self.store_backup_path}/.log/history.json"
        backup_history = {}

        if os.path.exists(write_path):
            with open(write_path, 'r', encoding='utf-8') as f:
                backup_history = json.load(f)
            self.logger.info(f"Read to backup history from [{write_path}]")

            history_num = len(backup_history)
            # 保留备份已满 -> 删 -> 增
            if history_num >= self.keep_history:
                self.remove_backup_history(backup_history)
            # 保留备份未满 -> 增
            elif history_num >= 1:
                pass
            else:
                self.logger.warning("There may have been an exception kill of the script before, ignored.")
        # 增：写入本次备份记录
        self.write_backup_history(backup_history, local_backup, remote_backup)
        with open(write_path, 'w', encoding='utf-8') as f:
            json.dump(backup_history, f, ensure_ascii=False, indent=2)
        self.logger.info(f"Saved this backup record to history [{write_path}]")

    def main(self):
        self.logger.debug("Google Drive Auto Backup Script Launch!")
        local_backup, remote_backup = self.backup_upload()
        self.manage_backup_history(local_backup, remote_backup)
        self.logger.debug("Script End~")

google_backup = GoogleDriveBackup()
google_backup.main()
