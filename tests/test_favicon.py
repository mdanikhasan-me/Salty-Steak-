import base64
import io
from email.message import Message
from PIL import Image
from app.backend.tooling import favicon

def test_favicon_is_rasterized_and_cached_without_remote_image_urls(monkeypatch):
    favicon._cache.clear()
    output=io.BytesIO();Image.new('RGB',(64,64),'red').save(output,format='PNG')
    calls=[]
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,n):return output.getvalue()
    class Opener:
        def open(self,*args,**kwargs):calls.append(True);return Response()
    monkeypatch.setattr(favicon,'checked_public_url',lambda url:url)
    monkeypatch.setattr(favicon.urllib.request,'build_opener',lambda *args:Opener())
    first=favicon.site_favicon('https://example.com/page');second=favicon.site_favicon('https://example.com/other')
    assert first==second and len(calls)==1
    assert first['icon'].startswith('data:image/png;base64,')
    image=Image.open(io.BytesIO(base64.b64decode(first['icon'].split(',')[1])));assert image.size==(32,32)

def test_nonpublic_favicon_inputs_are_not_fetched():
    assert favicon.site_favicon('file:///secret')=={'icon':None}
    assert favicon.site_favicon('https://user:pass@example.com')=={'icon':None}
