from datetime import datetime, timedelta
import json
import logging
from logging.config import dictConfig
import os
import pickle
import shutil
import stat
import threading
from time import sleep
import redis
import requests
import paramiko
import scp

from settings import Settings


class JobDictator(threading.Thread):
    def __init__(self):
        with open('logging.json') as jl:
            dictConfig(json.load(jl))
        threading.Thread.__init__(self)
        self.daemon = True
        self.headers = {'User-agent': 'dcs_wjc/1.0'}
        self.settings = Settings()
        self.client = redis.Redis('db')
        self.running = True
        logging.info('JobDictator: Starting.')

    def run(self):
        while self.running:
            self.aladeen()
            sleep(60)

    def aladeen(self):
        for job_id in [job_key for job_key in self.client.keys() if job_key.startswith('job-')]:  # Redis keys(pattern='*') does not filter at all.
            pickled_job = self.client.get(job_id)
            if pickled_job is None:
                continue  # self.client.keys('job-*') is stale
            job = pickle.loads(pickled_job)
            if job.state != 'booted' and job.state != 'running' and job.state != 'run_succeeded' and job.state != 'run_failed':
                continue
            #
            worker = None
            for worker_id in [worker_key for worker_key in self.client.keys() if worker_key.startswith('jm-')]:  # Redis keys(pattern='*') does not filter at all.
                pickled_worker = self.client.get(worker_id)
                if pickled_worker is not None:
                    temp_worker = pickle.loads(pickled_worker)
                    if temp_worker.job_id == job_id:
                        worker = temp_worker
                        break
            if worker is None:
                logging.error('Worker of active job %s not found, failing job.' % job_id)
                job.state = 'failed'
                self.client.set(job_id, pickle.dumps(job))
                continue
            #
            if job.state == 'booted':
                # check if state is ok
                ami_status = requests.get('http://%s/ilm/ami/%s/status' % (self.settings.web, worker.instance),
                                          headers=self.headers)
                if 'status:ok' not in ami_status.content.lower():
                    logging.info('AMI (%s) status (%s) NOK, waiting...' % (worker.instance, ami_status.content))
                    continue
                self.push(job.ami, job.batch_id, job_id, worker)
            elif job.state == 'run_succeeded' or job.state == 'run_failed':
                self.pull(job.ami, job.batch_id, job_id, worker)
                if job.state == 'run_succeeded':
                    job.state = 'finished'
                elif job.state == 'run_failed':
                    job.state = 'failed'
                self.client.set(job_id, pickle.dumps(job))
                self.client.publish('jobs', job_id)
            elif job.state == 'running':
                if job.run_started_on is None:
                    logging.info('JobDictator: Found a new running job %s.' % job_id)
                    job.run_started_on = datetime.now()
                    self.client.set(job_id, pickle.dumps(job))
                elif datetime.now() - job.run_started_on > timedelta(minutes=int(self.settings.job_timeout)):
                    job.state = 'broken'
                    self.client.set(job_id, pickle.dumps(job))
                    self.client.publish('jobs', job_id)

    def push(self, ami, batch_id, job_id, worker):
        logging.info('found job %s to transmit to worker, preparing script' % job_id)
        with open('ramon.py', 'r') as r:
            ramon = r.read()
            ramon = ramon.replace('[web]', self.settings.web)
            ramon = ramon.replace('[elk]', self.settings.elk)
            ramon = ramon.replace('[uuid]', job_id)
            ramon_file = '%s.sh' % job_id
            with open(ramon_file, 'w') as smooth:
                smooth.writelines(ramon)
            st_fn = os.stat(ramon_file)
            os.chmod(ramon_file, st_fn.st_mode | stat.S_IEXEC)
            logging.debug('script %s prepared' % ramon_file)
            # fish ami
            username, key_file = pickle.loads(self.client.get(ami))
            with paramiko.SSHClient() as ssh:
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                logging.info('establishing connection to push to %s using user %s' % (worker.ip_address, username))
                with open('%s.key' % job_id, 'wb') as hairy:
                    hairy.write(key_file)
                try:
                    ssh.connect(hostname=worker.ip_address, username=username, key_filename='%s.key' % job_id)
                    with scp.SCPClient(ssh.get_transport()) as s_scp:
                        luke = '/tmp/store/%s/%s' % (batch_id, job_id)
                        ssh.exec_command('mkdir %s' % job_id)
                        here = os.getcwd()
                        os.chdir(luke)
                        s_scp.put('.', job_id, recursive=True)
                        os.chdir(here)
                        s_scp.put(ramon_file, ramon_file)
                    ssh.exec_command('chmod +x %s' % ramon_file)
                    start = 'virtualenv venv\nsource venv/bin/activate\npip install python-logstash requests\nnohup ./%s  > /dev/null 2>&1 &\n' % ramon_file
                    logging.debug('calling remote start with %s' % start)
                    _, out, err = ssh.exec_command(start)
                    output = out.readlines()
                    error = err.readlines()
                    if output:
                        logging.info('%s output: %s' % (job_id, output))
                    if error:
                        logging.error('%s error: %s' % (job_id, error))
                        raise RuntimeError('error while executing remote run')
                    logging.info('started %s on %s' % (job_id, worker.instance))
                except Exception as e:
                    logging.error('Error in push: %s' % e.message)
                    logging.warning('Fatal error while starting job %s on worker %s, clean up manually.' % (job_id, worker.instance))
            os.remove(ramon_file)

    def pull(self, ami, batch_id, job_id, worker, clean=True, failed=False):
        destination = '/tmp/store/%s/%s' % (batch_id, job_id)
        if os.path.exists(destination):
            try:
                shutil.rmtree(destination)
            except:
                logging.error('Failed to remove directory tree %s' % destination)
                return
        if failed:
            destination += '_failed'
        with paramiko.SSHClient() as ssh:
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            username, key_file = pickle.loads(self.client.get(ami))
            logging.info('establishing connection to pull from %s using user %s' % (worker.ip_address, username))
            with open('%s.key' % job_id, 'wb') as hairy:
                hairy.write(key_file)
            try:
                ssh.connect(hostname=worker.ip_address, username=username, key_filename='%s.key' % job_id)
                with scp.SCPClient(ssh.get_transport()) as s_scp:
                    s_scp.get(job_id, destination, recursive=True)
                if clean:
                    ssh.exec_command('rm -rf %s' % job_id)
                    ssh.exec_command('rm -f %s.sh' % job_id)
                logging.info('transferred results for %s, saved to %s' % (job_id, destination))
            except Exception as e:
                logging.error('Error in pull: %s' % e.message)
                logging.warning('Fatal error while retrieving job %s on worker %s, clean up manually.' % (job_id, worker.instance))
