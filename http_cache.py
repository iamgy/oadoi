import os
import sys
import re
import hashlib
import json
import requests
import socket
import boto
import requests
import shutil
from requests.auth import HTTPProxyAuth
from requests.packages.urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from time import time
from time import sleep
from HTMLParser import HTMLParser
import inspect

from app import logger
from urlparse import urljoin, urlparse
from util import clean_doi
from util import get_tree
from util import get_link_target
from util import elapsed
from util import NoDoiException
from util import DelayedAdapter
from util import is_same_publisher

MAX_PAYLOAD_SIZE_BYTES = 1000*1000*10 # 10mb

def is_response_too_large(r):
    if not "Content-Length" in r.headers:
        # logger.info(u"can't tell if page is too large, no Content-Length header {}".format(r.url))
        return False

    content_length = r.headers["Content-Length"]
    # if is bigger than 25 MB, don't keep it don't parse it, act like we couldn't get it
    # if doing 100 in parallel, this would be 100MB, which fits within 512MB dyno limit
    if int(content_length) >= (25 * 1000 * 1000):
        logger.info(u"Content Too Large on GET on {url}".format(url=r.url))
        return True
    return False

# 10.2514/6.2006-5946!  https://arc.aiaa.org/doi/pdf/10.2514/6.2006-5946
# 10.3410/f.6269956.7654055 none
# 10.2514/6.2006-2106 none  (lots of redirects)
# 10.5040/9780567662088.part-003 none (book)
# 10.1016/j.jvcir.2016.03.027 (elsevier, relative links)
# 10.1002/(sici)1096-911x(200006)34:6<432::aid-mpo10>3.0.co;2-1 (has a blank tdm_api)
# python update.py Crossref.run_with_hybrid --id=10.2514/6.2006-5946


def keep_redirecting(r, publisher):
    # don't read r.content unless we have to, because it will cause us to download the whole thig instead of just the headers

    # 10.5762/kais.2016.17.5.316
    if ("content-length" in r.headers):
        # manually follow javascript if that's all that's in the payload
        file_size = int(r.headers["content-length"])
        if file_size < 500:
            matches = re.findall(ur"<script>location.href='(.*)'</script>", r.content_small(), re.IGNORECASE)
            if matches:
                redirect_url = matches[0]
                if redirect_url.startswith(u"/"):
                    redirect_url = get_link_target(redirect_url, r.url)
                return redirect_url

    # 10.1097/00003643-201406001-00238
    if publisher and is_same_publisher(publisher, "Ovid Technologies (Wolters Kluwer Health)"):
        matches = re.findall(ur"OvidAN = '(.*?)';", r.content_small(), re.IGNORECASE)
        if matches:
            an_number = matches[0]
            redirect_url = "http://content.wkhealth.com/linkback/openurl?an={}".format(an_number)
            return redirect_url

    # 10.1097/01.xps.0000491010.82675.1c
    hostname = urlparse(r.url).hostname
    if hostname and hostname.endswith('ovid.com'):
        matches = re.findall(ur'var journalURL = "(.*?)";', r.content_small(), re.IGNORECASE)
        if matches:
            journal_url = matches[0]
            logger.info(u'ovid journal match. redirecting to {}'.format(journal_url))
            return journal_url

    # handle meta redirects
    redirect_re = re.compile(u'<meta[^>]*http-equiv="?refresh"?[^>]*>', re.IGNORECASE | re.DOTALL)
    redirect_match = redirect_re.findall(r.content_small())
    if redirect_match:
        redirect = redirect_match[0]
        logger.info('found a meta refresh element: {}'.format(redirect))
        url_re = re.compile('url=["\']?([^">\']*)', re.IGNORECASE | re.DOTALL)
        url_match = url_re.findall(redirect)

        if url_match:
            redirect_path = HTMLParser().unescape(url_match[0].strip())
            redirect_url = urljoin(r.request.url, redirect_path)
            if not redirect_url.endswith('Error/JavaScript.html'):
                logger.info(u"redirect_match! redirecting to {}".format(redirect_url))
                return redirect_url

    return None

class RequestWithFileDownload(object):

    def content_small(self):
        return self.content_big()

        # if hasattr(self, "content_read"):
        #     return self.content_read
        #
        # self.content_read = self.content
        # return self.content_read


    def content_big(self):
        if hasattr(self, "content_read"):
            return self.content_read

        if not self.raw:
            self.content_read = self.content
            return self.content_read

        megabyte = 1024*1024
        maxsize = 25 * megabyte
        self.content_read = b""
        for chunk in self.iter_content(megabyte):
            self.content_read += chunk
            if len(self.content_read) > maxsize:
                logger.info(u"webpage is too big at {}, only getting first {} bytes".format(self.request.url, maxsize))
                self.close()
                return self.content_read
        return self.content_read


def get_session_id():
    return None

def request_ua_headers():
    return {
        'User-Agent': 'Unpaywall (http://unpaywall.org/; mailto:team@impactstory.org)',
        'From': 'team@impactstory.org',
    }

def call_requests_get(url,
                      headers={},
                      read_timeout=60,
                      connect_timeout=60,
                      stream=False,
                      publisher=None,
                      session_id=None,
                      ask_slowly=False,
                      verify=False,
                      cookies=None):

    if 'User-Agent' not in headers:
        headers['User-Agent'] = request_ua_headers()['User-Agent']

    if 'From' not in headers:
        headers['From'] = request_ua_headers()['From']

    following_redirects = True
    num_redirects = 0
    while following_redirects:
        requests_session = requests.Session()

        if ask_slowly:
            retries = Retry(total=1,
                            backoff_factor=0.1,
                            status_forcelist=[500, 502, 503, 504])
        else:
            retries = Retry(total=0,
                            backoff_factor=0.1,
                            status_forcelist=[500, 502, 503, 504])
        requests_session.mount('http://', DelayedAdapter(max_retries=retries))
        requests_session.mount('https://', DelayedAdapter(max_retries=retries))

        if u"citeseerx.ist.psu.edu/" in url:
            url = url.replace("http://", "https://")
            proxy_url = os.getenv("STATIC_IP_PROXY")
            proxies = {"https": proxy_url, "http": proxy_url}
        else:
            proxies = {}

        # logger.info(u"getting url {}".format(url))
        r = requests_session.get(url,
                    headers=headers,
                    timeout=(connect_timeout, read_timeout),
                    stream=stream,
                    proxies=proxies,
                    allow_redirects=True,
                    verify=verify,
                    cookies=cookies)

        # from http://jakeaustwick.me/extending-the-requests-response-class/
        for method_name, method in inspect.getmembers(RequestWithFileDownload, inspect.ismethod):
            setattr(requests.models.Response, method_name, method.im_func)

        if r and not r.encoding:
            r.encoding = "utf-8"

        # check to see if we actually want to keep redirecting, using business-logic redirect paths
        following_redirects = False
        num_redirects += 1
        if (r.status_code == 200) and (num_redirects < 5):
            redirect_url = keep_redirecting(r, publisher)
            if redirect_url:
                following_redirects = True
                url = redirect_url

    return r


def http_get(url,
             headers={},
             read_timeout=60,
             connect_timeout=60,
             stream=False,
             cache_enabled=False,
             allow_redirects=True,
             publisher=None,
             session_id=None,
             ask_slowly=False,
             verify=False,
             cookies=None):

    start_time = time()

    # reset
    os.environ["HTTP_PROXY"] = ""

    try:
        logger.info(u"LIVE GET on {}".format(url))
    except UnicodeDecodeError:
        logger.info(u"LIVE GET on an url that throws UnicodeDecodeError")

    max_tries = 2
    if ask_slowly:
        max_tries = 3
    success = False
    tries = 0
    r = None
    while not success:
        try:
            r = call_requests_get(url,
                                  headers=headers,
                                  read_timeout=read_timeout,
                                  connect_timeout=connect_timeout,
                                  stream=stream,
                                  publisher=publisher,
                                  session_id=session_id,
                                  ask_slowly=ask_slowly,
                                  verify=verify,
                                  cookies=cookies)
            success = True
        except (KeyboardInterrupt, SystemError, SystemExit):
            raise
        except Exception as e:
            # don't make this an exception log for now
            logger.info(u"exception in call_requests_get")
            tries += 1
            if tries >= max_tries:
                logger.info(u"in http_get, tried too many times, giving up")
                raise
            else:
                logger.info(u"in http_get, got an exception, trying again")
        finally:
            logger.info(u"finished http_get for {} in {} seconds".format(url, elapsed(start_time, 2)))

    return r

