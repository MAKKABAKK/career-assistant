"""Read-only page diagnostics and full-description extraction; no access bypass."""
import json
import time
from html.parser import HTMLParser
from urllib.parse import urlsplit
from .base import NeedsManualVerifyError


class AccessRequired(NeedsManualVerifyError):
    def __init__(self, source, reason):
        super().__init__(source)
        self.args = (f'{source}：{reason}；停止自动尝试，请在原网站人工处理或粘贴完整 JD。',)


class DescriptionError(ValueError):
    pass


def check_access(page, source, status=200):
    path = urlsplit(page.url).path.lower()
    if status in (401, 403, 429):
        raise AccessRequired(source, f'访问限制 HTTP {status}')
    if any(x in path for x in ('/login', '/checkpoint', '/challenge', 'authwall', '/signin')):
        raise AccessRequired(source, '登录失效或需要验证')
    # Check visible challenge headings, not arbitrary job text mentioning security.
    heading = page.locator('title,h1,h2,[role="alert"]').all_text_contents()
    text = ' '.join(heading).casefold()
    if any(x in text for x in ('verify you are human','just a moment','access denied','security verification','確認您是真人','验证您是真人','unusual traffic')) or page.locator('iframe[src*="captcha"],#challenge-running,input[name="cf-turnstile-response"]').count():
        raise AccessRequired(source, '验证码或访问验证页面')


class PlainText(HTMLParser):
    def __init__(self): super().__init__(); self.parts=[]; self.hidden=0
    def handle_starttag(self, tag, attrs):
        if tag in ('script','style'): self.hidden += 1
    def handle_endtag(self, tag):
        if tag in ('script','style'): self.hidden = max(0,self.hidden-1)
    def handle_data(self, data):
        if not self.hidden and data.strip(): self.parts.append(data.strip())


def extract_description(page, snapshot):
    postings=[]
    def walk(value):
        if isinstance(value,list):
            for item in value:walk(item)
        elif isinstance(value,dict):
            kind=value.get('@type',[])
            if kind=='JobPosting' or isinstance(kind,list) and 'JobPosting' in kind:
                if isinstance(value.get('description'),str):postings.append(value)
            if '@graph' in value:walk(value['@graph'])
    for raw in page.locator('script[type="application/ld+json"]').all_text_contents():
        try:walk(json.loads(raw))
        except (ValueError,TypeError):pass
    matched=[p for p in postings if p.get('url','').rstrip('/')==snapshot['url'].rstrip('/')]
    candidates=matched or postings
    descriptions=list(dict.fromkeys(p['description'] for p in candidates))
    if len(descriptions)>1:
        raise DescriptionError('解析歧义：页面含多个不同岗位正文，请粘贴所选岗位完整 JD。')
    if len(descriptions)==1:
        if len(candidates)==1 and candidates[0].get('url') and not matched:
            raise DescriptionError('结构化岗位链接不匹配，请粘贴完整 JD。')
        parser=PlainText();parser.feed(descriptions[0]);text='\n'.join(parser.parts)
        if text:return text
    selectors={'linkedin':'.show-more-less-html__markup, .jobs-description__content .jobs-box__html-content',
               'jobsdb':'[data-automation="jobAdDetails"]', 'indeed':'#jobDescriptionText'}
    values=list(dict.fromkeys(t.strip() for t in page.locator(selectors[snapshot['source']]).all_inner_texts() if t.strip()))
    if len(values)==1:return values[0]
    if len(values)>1:raise DescriptionError('解析歧义：多个详情正文，请粘贴完整 JD。')
    return None


def wait_description(page, snapshot, timeout=10):
    deadline=time.monotonic()+timeout
    expanded = False
    while True:
        check_access(page,snapshot['source'])
        more = page.locator('.show-more-less-html__button--more, .jobs-description__footer-button')
        if more.count() and more.first.is_visible():
            if expanded:
                raise DescriptionError('详情仍处于折叠状态，请打开原链接或粘贴完整 JD。')
            more.first.click(timeout=3000)
            expanded = True
            page.wait_for_timeout(250)
            continue
        text=extract_description(page,snapshot)
        if text:return text
        if time.monotonic()>=deadline:
            raise DescriptionError('动态加载超时或解析规则失效：未取得完整正文；请粘贴 JD。')
        page.wait_for_timeout(250)
