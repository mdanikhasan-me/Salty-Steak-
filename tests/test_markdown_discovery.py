from email.message import Message
from io import BytesIO
from types import SimpleNamespace

from app.backend.tooling import web_page


def read_markdown(monkeypatch, content):
    class Response(BytesIO):
        status = 200
        headers = Message()
        headers['Content-Type'] = 'text/markdown; charset=utf-8'
        def geturl(self): return 'https://docs.example/guide/index.md'
    monkeypatch.setattr(web_page, 'checked_public_url', lambda value:value)
    monkeypatch.setattr(web_page, 'public_opener', lambda:SimpleNamespace(
        open=lambda *a, **k:Response((content + '\n\n' + 'Visible explanatory prose. '*10).encode())))
    return web_page.read_public_page('https://docs.example/guide/index.md')


def test_markdown_nested_destinations_and_reference_links(monkeypatch):
    page = read_markdown(monkeypatch, '''[Report **edition**](../report_(final).pdf "Original report")
[Section][detail]

[detail]: /reference?q=a&b=2 "Reference title"

<https://original.example/archive>
''')
    assert page['links'] == [
        {'url':'https://docs.example/report_(final).pdf','title':'Report edition'},
        {'url':'https://docs.example/reference?q=a&b=2','title':'Section'},
        {'url':'https://original.example/archive','title':'https://original.example/archive'},
    ]


def test_markdown_code_images_unsafe_and_fragment_links_are_not_discovery(monkeypatch):
    page = read_markdown(monkeypatch, '''`[inline example](https://not-a-source.example)`
```markdown
[code example](https://not-a-source.example/code)
```
![picture](https://images.example/photo.png)
[mail](mailto:help@example.com) [local](file:///private) [anchor](#part)
[actual](/article) [again](/article)
''')
    assert page['links'] == [{'url':'https://docs.example/article','title':'actual'}]
