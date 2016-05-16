#!/usr/bin/env python3
# -*- coding: utf-8 -*-

'''
IME bot - Rime IME online
'''

import os
import re
import sys
import time
import json
import queue
import base64
import logging
import hashlib
import requests
import functools
import threading
import subprocess
import concurrent.futures

import zhconv
import simpleime

logging.basicConfig(stream=sys.stderr, format='%(asctime)s [%(name)s:%(levelname)s] %(message)s', level=logging.DEBUG if sys.argv[-1] == '-v' else logging.INFO)

logger_botapi = logging.getLogger('botapi')

executor = concurrent.futures.ThreadPoolExecutor(5)
HSession = requests.Session()

class AttrDict(dict):

    def __init__(self, *args, **kwargs):
        super(AttrDict, self).__init__(*args, **kwargs)
        self.__dict__ = self

class BotAPIFailed(Exception):
    pass

def async_func(func):
    @functools.wraps(func)
    def wrapped(*args, **kwargs):
        def func_noerr(*args, **kwargs):
            try:
                func(*args, **kwargs)
            except Exception:
                logger_botapi.exception('Async function failed.')
        executor.submit(func_noerr, *args, **kwargs)
    return wrapped

def bot_api(method, **params):
    for att in range(3):
        try:
            req = HSession.get(('https://api.telegram.org/bot%s/' %
                                CFG.apitoken) + method, params=params, timeout=45)
            retjson = req.content
            ret = json.loads(retjson.decode('utf-8'))
            break
        except Exception as ex:
            if att < 1:
                time.sleep((att + 1) * 2)
            else:
                raise ex
    if not ret['ok']:
        raise BotAPIFailed(repr(ret))
    return ret['result']

@async_func
def sendmsg(text, chat_id, reply_to_message_id=None):
    text = text.strip()
    if not text:
        logger_botapi.warning('Empty message ignored: %s, %s' % (chat_id, reply_to_message_id))
        return
    logger_botapi.info('sendMessage(%s): %s' % (len(text), text[:20]))
    if len(text) > 2000:
        text = text[:1999] + '…'
    return bot_api('sendMessage', chat_id=chat_id, text=text, reply_to_message_id=reply_to_message_id)

@async_func
def answer(inline_query_id, results, **kwargs):
    return bot_api('answerInlineQuery', inline_query_id=inline_query_id, results=json.dumps(results), **kwargs)

def updatebotinfo():
    global CFG
    d = bot_api('getMe')
    CFG['username'] = d.get('username')

def getupdates():
    global CFG
    while 1:
        try:
            updates = bot_api('getUpdates', offset=CFG['offset'], timeout=10)
        except Exception:
            logger_botapi.exception('Get updates failed.')
            continue
        if updates:
            #logger_botapi.debug('Messages coming: %r', updates)
            CFG['offset'] = updates[-1]["update_id"] + 1
            for upd in updates:
                MSG_Q.put(upd)
        time.sleep(.2)

def parse_cmd(text: str):
    t = text.strip().replace('\xa0', ' ').split(' ', 1)
    if not t:
        return (None, None)
    cmd = t[0].rsplit('@', 1)
    if len(cmd[0]) < 2 or cmd[0][0] != "/":
        return (None, None)
    if len(cmd) > 1 and 'username' in CFG and cmd[-1] != CFG['username']:
        return (None, None)
    expr = t[1] if len(t) > 1 else ''
    return (cmd[0][1:], expr.strip())

articleid = lambda s: base64.b64encode(hashlib.sha256(s.encode('utf-8')).digest()).decode('ascii')

def handle_api_update(d: dict):
    logger_botapi.debug('Update: %r' % d)
    try:
        if 'inline_query' in d:
            query = d['inline_query']
            text = query['query'].strip()
            imeresult = simpleime_input(text)
            if imeresult:
                r = answer(query['id'], [{'type': 'article', 'id': articleid(ret), 'title': ret, 'input_message_content': {'message_text': ret}, 'description': desc} for ret, desc in imeresult])
                logger_botapi.debug(r)
                logger_botapi.info('%s -> %s', text, imeresult)
        elif 'message' in d:
            msg = d['message']
            if msg['chat']['type'] == 'private':
                text = msg.get('text', '').strip()
                if text == '/start':
                    response = 'This is an inline bot. Send me text directly or use @' + CFG['username']
                else:
                    imeresult = simpleime_input(msg.get('text', '').strip())
                    if imeresult:
                        response = imeresult[0][0]
                if response:
                    sendmsg(response, msg['chat']['id'], msg['message_id'])
    except Exception:
        logger_botapi.exception('Failed to process a message.')

def simpleime_input(text: str):
    if not text:
        return []
    results = []
    result = simpleime.pinyininput(text)
    results.append((zhconv.convert(result, 'zh-hans'), 'Simple IME (zh-hans)'))
    results.append((result, 'Simple IME (zh-hant)'))
    return results

def rime_input(text: str):
    global RIME_P
    if not text:
        return ''
    with RIME_LCK:
        logging.debug(text)
        text = text.encode('utf-8') + b'\n'
        try:
            RIME_P.stdin.write(text)
            RIME_P.stdin.flush()
            result = RIME_P.stdout.readline().strip().decode('utf-8')
        except BrokenPipeError:
            RIME_P = subprocess.Popen(RIME_CMD, stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd='data')
            RIME_P.stdin.write(text)
            RIME_P.stdin.flush()
            result = RIME_P.stdout.readline().strip().decode('utf-8')
        logging.debug(result)
    return result

def load_config():
    return AttrDict(json.load(open('config.json', encoding='utf-8')))

def save_config():
    json.dump(CFG, open('config.json', 'w'), sort_keys=True, indent=1)

if __name__ == '__main__':
    CFG = load_config()
    MSG_Q = queue.Queue()
    simpleime.loaddict()
    try:
        updatebotinfo()
        apithr = threading.Thread(target=getupdates)
        apithr.daemon = True
        apithr.start()

        while 1:
            handle_api_update(MSG_Q.get())
    finally:
        save_config()
