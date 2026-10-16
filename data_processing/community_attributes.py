"""Evidence-based residential morphology and approximate completion dates."""
import re
from datetime import datetime, timezone

PROFILE_VERSION=5
SOURCE_TAGS=('landuse','building','residential','building:use','building:levels',
             'start_date','building:start_date','construction:date','opening_date',
             'source:start_date','source','heritage','heritage:operator','place','amenity','tourism','name:en','alt_name')
TYPE_ORDER=('别墅','楼房','公寓','自建房','独栋/联排住宅','乡村住宅')
RESIDENTIAL_SCOPES=('小区','分期')
VILLA_SUBTYPE_ORDER=('独栋','联排','双拼','叠拼')

def excluded_residence(tags,name):
    if tags.get('tourism') in ('hotel','hostel','guest_house','apartment'):return True
    if tags.get('amenity') in ('school','university','college','dormitory'):return True
    if any(set(tags.get(k,'').split(';')) & {'dormitory','student','university','student_accommodation'} for k in ('building','residential','building:use')):return True
    # User excludes dormitories from the community map; preserve raw evidence separately.
    return bool(re.search(r'宿舍|学生公寓|研究生公寓|留学生公寓|\bdormitor(?:y|ies)\b',name,re.I))


def year_value(value):
    text=str(value or '').strip()
    precision='year'
    approximate=bool(re.match(r'^(?:约|~|c\.?\s|circa\s)',text,re.I))
    text=re.sub(r'^(?:约|~|c\.?\s*|circa\s+)','',text,flags=re.I).strip()
    decade=re.fullmatch(r'(\d{3}0)(?:s|年代)',text)
    span=re.fullmatch(r'(\d{4})\s*(?:年)?\s*(?:\.\.|至|到|[-–—/])\s*(\d{4})年?',text)
    exact=re.fullmatch(r'(\d{4})(?:年|(?:-\d{2}){1,2})?',text)
    if decade:start=int(decade[1]);end=start+9;precision='decade'
    elif span:start,end=map(int,span.groups());precision='range'
    elif exact:start=end=int(exact[1]);precision='approximate' if approximate else 'year'
    else:return None
    if not (1600<=start<=end<=datetime.now(timezone.utc).year):return None
    label=f'{start}年代' if precision=='decade' else f'{start}-{end}年' if start!=end else ('约' if approximate else '')+f'{start}年'
    return dict(start=start,end=end,label=label,precision=precision)


def summarize_years(evidence):
    valid=[e for e in evidence if e.get('start') and e.get('end') and e.get('scope') in RESIDENTIAL_SCOPES]
    if not valid:return dict(status='unknown',label='暂缺',start=None,end=None,scope=None,evidence=[])
    preferred=valid
    scopes={e['scope'] for e in preferred}
    starts={e['start'] for e in preferred};ends={e['end'] for e in preferred}
    if len(starts)>1 or len(ends)>1:
        return dict(status='conflict',label='年份待核',start=None,end=None,scope=' / '.join(sorted(scopes)),evidence=valid)
    e=preferred[0]
    return {k:e[k] for k in ('start','end','label','precision','scope')}|dict(status='recorded',evidence=valid)


def osm_attributes(tags,name,url,fetched_at):
    kinds=[];evidence=[]
    def add(kind,field,value,level='tag'):
        if kind not in kinds:kinds.append(kind)
        evidence.append(dict(kind=kind,field=field,value=value,source='OpenStreetMap',source_url=url,
                             fetched_at=fetched_at,confidence=level))
    # OSM apartments means multi-family housing, not the Chinese market category 公寓.
    for field in ('building','residential'):
        for value in tags.get(field,'').split(';'):
            if value in ('apartments','condominium'):add('楼房',field,value)
            elif value=='villa':add('别墅',field,value)
            elif value in ('house','detached','semidetached_house','terrace','duplex','single_family','cottage'):add('独栋/联排住宅',field,value)
            elif value in ('rural','irregular_settlement'):add('乡村住宅',field,value)
    if re.search(r'公寓|\bapartments?\b',name,re.I):add('公寓','name',name,'name_hint')
    if re.search(r'别墅|\bvillas?\b',name,re.I):add('别墅','name',name,'name_hint')
    if '自建房' in name:add('自建房','name',name,'name_hint')
    years=[]
    # Only residential-area dates qualify. A building's date never fills its community's year.
    for field in ('start_date',) if tags.get('landuse')=='residential' and tags.get('building') in (None,'no') else ():
        parsed=year_value(tags.get(field))
        if parsed:
            years.append(parsed|dict(scope='小区',source='OpenStreetMap',source_url=url,field=field,
                value=tags[field],fetched_at=fetched_at,note='住宅片区建成/启用年代标注，非竣工验收证明'))
    return finalize_types(kinds,evidence)|dict(completion_year=summarize_years(years))


def finalize_types(kinds,evidence,pure_villa=False):
    # Marketing names are discovery hints, not evidence of a mixed residential compound.
    supported={e['kind'] for e in evidence if e.get('confidence')!='name_hint'}
    kinds=[k for k in TYPE_ORDER if k in supported]
    # 公寓 is a named subtype of multi-family housing; do not display it as two separate forms.
    visible=[k for k in kinds if not (k=='楼房' and '公寓' in kinds and '别墅' not in kinds)]
    pure_villa=bool(pure_villa and set(kinds)=={'别墅'})
    label='+'.join(visible) or '住宅（形态待核）'
    hints=sorted({e['kind'] for e in evidence if e.get('confidence')=='name_hint'}-
                 {e['kind'] for e in evidence if e.get('confidence')!='name_hint'})
    subtype_text=' '.join(str(e.get(k,'')) for e in evidence if e.get('kind')!='独栋/联排住宅' for k in ('kind','value','raw_text'))
    villa_subtypes=[s for s in VILLA_SUBTYPE_ORDER if s in subtype_text]
    return dict(type=label,housing_types=kinds,pure_villa=pure_villa,type_evidence=evidence,
                villa_subtypes=villa_subtypes,
                type_hints=hints,type_basis='名称线索：'+'、'.join(hints)+'；其余以来源标签为准' if hints else '来源明确标签；不代表产权或房屋状况')


def market_types(text):
    """Only classify explicit property-form terms, never 住宅 alone or rural as self-built."""
    kinds=[]
    if re.search(r'别墅|独栋|联排|双拼|叠拼',text):kinds.append('别墅')
    if re.search(r'板楼|塔楼|板塔结合|多层|小高层|高层|楼房',text):kinds.append('楼房')
    if '公寓' in text:kinds.append('公寓')
    if '自建房' in text:kinds.append('自建房')
    return kinds, bool(re.search(r'纯别墅',text)) and kinds==['别墅']
