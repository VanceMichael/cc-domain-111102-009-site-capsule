from html.parser import HTMLParser
from urllib.parse import urljoin, urldefrag

class References(HTMLParser):
    def __init__(self):
        super().__init__();self.base=None;self.links=[]
    def handle_starttag(self,tag,attrs):
        values=dict(attrs)
        if tag=="base" and self.base is None: self.base=values.get("href")
        for key in ("href","src"):
            if key in values: self.links.append((tag,key,values[key]))

def inspect_links(html,document_url):
    p=References();p.feed(html);p.close()
    base=urljoin(document_url,p.base) if p.base else document_url
    result=[]
    for tag,attr,raw in p.links:
        resolved=urljoin(base,raw);fetch,fragment=urldefrag(resolved)
        result.append({"tag":tag,"attribute":attr,"raw":raw,"resolved":resolved,"fetch":fetch,"fragment":fragment})
    return result
