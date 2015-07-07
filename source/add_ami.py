import argparse
import json
import os
import requests

parser = argparse.ArgumentParser(description='add a new ami to the system.')

parser.add_argument('server', help='public ip of the web server')
parser.add_argument('ami', help='ami id')
parser.add_argument('username', help='username with sufficient rights to run the program and write to home')
parser.add_argument('private_key', help='path to the pem file')

args = parser.parse_args()

args = args.__dict__

if not os.path.exists(args['private_key']):
    raise Exception('Private key does not exist ' + args['private_key'])
with open(args['private_key'], 'rb') as pk:
    data = json.dumps({'name': args['ami'], 'username': args['username'], 'private_key': pk.read()})
    headers = {'Content-Type': 'application/json'}
    r = requests.post('http://%s/ilm/amis' % args['server'], data=data, headers=headers)
    if r.status_code != 200:
        raise Exception('Status code not 200, %s' % r.content)
    print 'Added AMI'
