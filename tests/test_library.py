"""All text bodies are invented fixtures; no real EPUB is needed or opened."""
from copy import deepcopy
import json
from pathlib import Path
import zipfile
from xml.sax.saxutils import escape
import pytest

from tools.monogatari import catalog, draw, paginate, validate_cards as vc
from tools.monogatari.epub_text import read_epub, resolve_ref
from tools.monogatari.lookup import lookup


def epub_fixture(path, title="伤物语", arcs=None, *, nav=False, folder="Text", anchors=False):
    arcs = arcs or ["第零话 历·吸血鬼"]
    package = "OPS/package.opf"
    items, refs, entries, payloads = [], [], [], {}
    if anchors:
        body = []
        for i, arc in enumerate(arcs):
            body.append(f'<h1 id="a{i}">{escape(arc)}</h1><p>' + ("这是测试用的虚构句子，纸船沿着小溪漂走。" * 180) + '</p>')
            entries.append((arc, f"{folder}/joined.xhtml#a{i}"))
        payloads[f"{folder}/joined.xhtml"] = '<html><body>' + ''.join(body) + '</body></html>'
    else:
        for i, arc in enumerate(arcs):
            target = f"{folder}/part{i}.xhtml"
            payloads[target] = f'<html><body><h1>{escape(arc)}</h1><p>' + ("这是测试用的虚构句子，纸船沿着小溪漂走。" * 180) + '</p></body></html>'
            entries.append((arc, target))
    for i, target in enumerate(payloads):
        items.append(f'<item id="s{i}" href="{target}" media-type="application/xhtml+xml"/>')
        refs.append(f'<itemref idref="s{i}"/>')
    if nav:
        items.append('<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>')
        payloads['nav.xhtml'] = '<html xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol>' + ''.join(f'<li><a href="{t}">{escape(a)}</a></li>' for a,t in entries) + '</ol></nav></body></html>'
    else:
        items.append('<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>')
        payloads['toc.ncx'] = '<ncx><navMap>' + ''.join(f'<navPoint id="n{i}"><navLabel><text>{escape(a)}</text></navLabel><content src="{t}"/></navPoint>' for i,(a,t) in enumerate(entries)) + '</navMap></ncx>'
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('META-INF/container.xml', '<container><rootfiles><rootfile full-path="OPS/package.opf"/></rootfiles></container>')
        z.writestr(package, '<package><metadata><title>' + escape(title) + '</title></metadata><manifest>' + ''.join(items) + '</manifest><spine toc="ncx">' + ''.join(refs) + '</spine></package>')
        for name, value in payloads.items(): z.writestr('OPS/' + name, value)
    return path


@pytest.fixture
def library(tmp_path):
    books=tmp_path/'epubs';books.mkdir()
    epub_fixture(books/'arbitrary-file.epub')
    conf={'epub_dir':str(books),'state_dir':str(tmp_path/'state'),'log_path':str(tmp_path/'log.jsonl'),
          'target_chars':1100,'merge_tail_below':400,'fallback_threshold':1600,'include_books':['伤物语/-']}
    path=tmp_path/'config.json';path.write_text(json.dumps(conf),encoding='utf8')
    manifest,index=paginate.build(conf,scan_all=True)
    for p,obj in zip(paginate.state_paths(conf),(manifest,index)):
        p.parent.mkdir(exist_ok=True);p.write_bytes(paginate.encode_json(obj))
    return conf,path,manifest,index


@pytest.mark.parametrize('nav',[False,True])
@pytest.mark.parametrize('anchors',[False,True])
def test_metadata_toc_and_internal_layout(tmp_path,nav,anchors):
    arcs=['第一话 黑仪·重蟹','第二话 真宵·蜗牛','第三话 骏河·猴子']
    path=epub_fixture(tmp_path/'unrelated.epub','化物语（上）',arcs,nav=nav,folder='custom/deep',anchors=anchors)
    epub=catalog.identify(read_epub(path));assert(epub.book,epub.volume)==('化物语','上')
    meta,parts=paginate.prepare_book(epub,catalog.default_overrides(epub))
    assert len(parts)==3 and all(parts)
    assert [a['title'] for a in meta['arcs']]==arcs
    assert all('custom/deep' in a['start']['spine'] for a in meta['arcs'])


def test_toc_can_resolve_missing_metadata(tmp_path):
    e=catalog.identify(read_epub(epub_fixture(tmp_path/'x.epub','未知标题')))
    assert e.book=='伤物语'


def test_filename_cannot_resolve_ambiguity(tmp_path):
    e=read_epub(epub_fixture(tmp_path/'猫物语(白).epub','猫物语',['001']))
    with pytest.raises(ValueError,match='ambiguous'):catalog.identify(e)
    assert catalog.identify(e,['猫物语','白']).volume=='白'


def test_conflicting_metadata_and_toc_rejected(tmp_path):
    e=read_epub(epub_fixture(tmp_path/'x.epub','猫物语（黑）'))
    with pytest.raises(ValueError,match='conflict'):catalog.identify(e)


def test_partial_multi_arc_book_not_guessed(tmp_path):
    e=catalog.identify(read_epub(epub_fixture(tmp_path/'x.epub','化物语上',['第一话 黑仪·重蟹'])))
    with pytest.raises(ValueError,match='TOC arc'):paginate.prepare_book(e,catalog.default_overrides(e))


def test_alias_override_is_explicit(tmp_path):
    e=catalog.identify(read_epub(epub_fixture(tmp_path/'x.epub','伤物语',['本卷故事'])))
    rules=catalog.default_overrides(e)
    with pytest.raises(ValueError):paginate.prepare_book(e,rules)
    rules['arc_aliases']={'1':['本卷故事']}
    meta,parts=paginate.prepare_book(e,rules)
    assert meta['arcs'][0]['title']=='第零话 历·吸血鬼' and parts
    assert all(p.text != '本卷故事' for p in parts[0])


def test_repeat_build_and_rename(library):
    conf,path,m,i=library
    assert paginate.build(conf,m)==(m,i)
    old=Path(conf['epub_dir'])/m['books'][0]['epub'];old.rename(old.with_name('renamed.epub'))
    new_m,new_i=paginate.build(conf,scan_all=True)
    ignored={'epub'}
    assert [{k:v for k,v in p.items() if k not in ignored} for p in i['pages']]==[{k:v for k,v in p.items() if k not in ignored} for p in new_i['pages']]
    with pytest.raises(ValueError,match='missing'):paginate.build(conf,m)


def test_registry_ignores_unregistered_corrupt_file(library):
    conf,path,m,i=library
    (Path(conf['epub_dir'])/'unregistered.epub').write_bytes(b'not a zip')
    assert draw.load_verified(path)[1:3]==(m,i)


@pytest.mark.parametrize('change',['missing','tampered'])
def test_registry_blocks_before_entropy(library,monkeypatch,change):
    conf,path,m,i=library
    book=Path(conf['epub_dir'])/m['books'][0]['epub']
    if change=='missing':book.unlink()
    else:book.write_bytes(b'changed')
    monkeypatch.setattr(draw.secrets,'randbelow',lambda n:pytest.fail('entropy consumed'))
    assert draw.main(['--config',str(path),'draw','--question','测试'])==1
    assert not Path(conf['log_path']).exists()


def test_duplicate_edition_rejected(library):
    conf,_,m,_=library
    first=Path(conf['epub_dir'])/m['books'][0]['epub'];first.with_name('duplicate.epub').write_bytes(first.read_bytes())
    with pytest.raises(ValueError,match='duplicate edition'):paginate.build(conf,scan_all=True)


def test_index_drift_rejected(library):
    conf,path,m,i=library;i['pages'][0]['chars']+=1
    paginate.state_paths(conf)[1].write_bytes(paginate.encode_json(i))
    with pytest.raises(ValueError,match='changed'):draw.load_verified(path)


def test_manifest_digest_rejected(library):
    conf,path,m,i=library;m['review_decisions']['extra']='x'
    paginate.state_paths(conf)[0].write_bytes(paginate.encode_json(m))
    with pytest.raises(ValueError,match='mismatch'):draw.load_verified(path)


def test_no_following_page_context(library):
    conf,_,m,i=library
    payload=draw.page_payload(conf,m,i,i['pages'][1])
    assert payload['context_before'] and payload['context_after'] is None
    assert '后文语境' not in draw.format_page(payload)


def test_show_does_not_write_log(library,capsys):
    conf,path,_,_=library
    assert draw.main(['--config',str(path),'show','--page','1','--json'])==0
    assert not Path(conf['log_path']).exists()
    assert json.loads(capsys.readouterr().out)['global_page']==1


def test_draw_receipt_durable_before_output(library,monkeypatch,capsys):
    conf,path,m,i=library;calls=[]
    monkeypatch.setattr(draw.secrets,'randbelow',lambda n:(calls.append(n) or 0))
    original=draw.page_payload
    def payload(*args):
        receipt=json.loads(Path(conf['log_path']).read_text(encoding='utf8'))
        assert receipt['global_page']==1 and receipt['question']=='原问题'
        return original(*args)
    monkeypatch.setattr(draw,'page_payload',payload)
    assert draw.main(['--config',str(path),'draw','--question','原问题','--json'])==0
    output=json.loads(capsys.readouterr().out)
    assert output['receipt']['global_page']==1 and len(calls)==1


def test_failed_output_no_redraw(library,monkeypatch,capsys):
    conf,path,m,i=library;calls=[]
    monkeypatch.setattr(draw.secrets,'randbelow',lambda n:(calls.append(n) or 0))
    def fail(*args):raise OSError('output failed')
    monkeypatch.setattr(draw,'page_payload',fail)
    assert draw.main(['--config',str(path),'draw','--question','测试'])==1
    assert len(calls)==1 and 'show --page 1' in capsys.readouterr().err
    assert len(Path(conf['log_path']).read_text(encoding='utf8').splitlines())==1


def test_uncertain_journal_blocks_before_entropy(tmp_path):
    log=tmp_path/'log';log.write_bytes(b'{unfinished')
    with pytest.raises(ValueError,match='partial'):
        draw.commit_draw('q',[{}],'digest',log,chooser=lambda n:pytest.fail('entropy consumed'))


def test_fsync_failure_reports_consumed(library,monkeypatch,capsys):
    conf,_,m,i=library
    def fail(fd):raise OSError('disk failure')
    monkeypatch.setattr(draw.os,'fsync',fail)
    with pytest.raises(OSError):draw.commit_draw('q',i['pages'],'digest',Path(conf['log_path']),chooser=lambda n:0)
    assert '禁止重抽' in capsys.readouterr().err


def test_lookup_prior_range_and_rounding():
    card={'book':'测试物语','volume':'','arc_no':1,'arc':'测试话','anomaly':{},'protagonist':'测试员','themes':[],
          'stages':[{'name':'初段','from':0,'to':0.5,'summary':'测试开始。','cast':[]},{'name':'末段','from':0.5,'to':1,'summary':'测试继续。','cast':[]}]}
    cards={'cards':{'测试物语/-/1':card}}
    index={'pages':[{'global_page':n,'book':'测试物语','volume':'','book_page':n,'arc_id':1,'arc_page':n,'arc_total':4,'chars':100,'epub':'test'} for n in range(1,5)]}
    assert lookup(1,cards,index)['prior_pages'] is None
    result=lookup(4,cards,index)
    assert result['prior_pages']==[3,3] and result['page_in_stage']==2
    assert result['previous_stage']['name']=='初段'
    assert vc.progress_boundary(0.487,[0,109,224])==1


def test_structure_without_books():
    data=json.loads(vc.CARDS.read_text(encoding='utf8'))
    assert vc.validate_structure(data)==[]
    bad=deepcopy(data);next(iter(bad['cards'].values()))['stages'][0]['to']=0
    assert vc.validate_structure(bad)


def test_validator_explicit_skip(tmp_path,capsys):
    assert vc.main(['--config',str(tmp_path/'missing.json')])==0
    assert 'SKIP 12-character overlap' in capsys.readouterr().out


def test_twelve_character_overlap_across_pages():
    text='甲乙丙丁戊己庚辛壬癸子丑'
    hits=vc.overlap_hits([('summary',text)],[(1,text[:6]),(2,text[6:])],proper_nouns=())
    assert len(hits)==1 and hits[0]['page']==1 and text not in str(hits)
    assert vc.overlap_hits([('summary',text[:11])],[(1,text)],proper_nouns=())==[]


def test_reference_escape_rejected():
    with pytest.raises(ValueError):resolve_ref('OPS/package.opf','../../outside')


def test_pagination_merges_short_tail():
    p=paginate.Paragraph('fake',0,'甲'*1099+'。'+'乙'*99+'。')
    pages=paginate.paginate_arc([p],{'target_chars':1100,'merge_tail_below':400,'fallback_threshold':1600})
    assert len(pages)==1 and pages[0]['chars']==1200 and 'tail-merged' in pages[0]['exceptions']


def test_explicit_file_selection_does_not_open_other_books(library):
    conf,_,m,_=library
    (Path(conf['epub_dir'])/'not-selected.epub').write_bytes(b'not an epub')
    conf['epub_files']=[m['books'][0]['epub']]
    assert len(paginate.build(conf,scan_all=True)[0]['books'])==1


def test_changed_pagination_rules_rejected(library):
    conf,path,_,_=library;conf['target_chars']=1200
    path.write_text(json.dumps(conf),encoding='utf8')
    with pytest.raises(ValueError,match='changed'):draw.load_verified(path)


def test_card_private_name_guard_remains():
    data=json.loads(vc.CARDS.read_text(encoding='utf8'))
    next(iter(data['cards'].values()))['protagonist']='astarion'
    assert any('private name' in error for error in vc.validate_structure(data))
