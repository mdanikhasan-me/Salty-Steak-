from pathlib import Path
import base64,json,struct
base=Path('D:/Salty Steak');source=base/'validation/companion-tablet-20260907/Otter-Tablet-Companion.glb';target=base/'app/frontend/public/assets/companion'
b=source.read_bytes();n=struct.unpack_from('<I',b,12)[0];d=json.loads(b[20:20+n]);raw=b[28+n:]
# Blender omits static object curves from an NLA loop. Carry the final entrance
# pose explicitly, so crossfading off the entrance cannot restore hidden scale.
enter=next(a for a in d['animations'] if a['name']=='Work_Enter')
loop=next(a for a in d['animations'] if a['name']=='Work_Loop')
duration=max(d['accessors'][s['input']]['max'][0] for s in loop['samplers'])
def append_floats(values,kind,count,minimum=None,maximum=None):
    global raw
    raw+=b'\0'*((-len(raw))%4);offset=len(raw);chunk=struct.pack('<'+'f'*len(values),*values);raw+=chunk
    view=len(d['bufferViews']);d['bufferViews'].append({'buffer':0,'byteOffset':offset,'byteLength':len(chunk)})
    accessor={'bufferView':view,'componentType':5126,'count':count,'type':kind}
    if minimum is not None:accessor['min']=minimum;accessor['max']=maximum
    d['accessors'].append(accessor);return len(d['accessors'])-1
times=append_floats([0,duration],'SCALAR',2,[0],[duration])
for channel in enter['channels']:
    node=channel['target']['node'];prop=channel['target']['path']
    if d['nodes'][node].get('name')!='Tablet_Control':continue
    if any(c['target']==channel['target'] for c in loop['channels']):continue
    sampler=enter['samplers'][channel['sampler']];acc=d['accessors'][sampler['output']];view=d['bufferViews'][acc['bufferView']]
    components={'VEC3':3,'VEC4':4}[acc['type']];offset=view.get('byteOffset',0)+acc.get('byteOffset',0)+(acc['count']-1)*components*4
    value=list(struct.unpack_from('<'+'f'*components,raw,offset));output=append_floats(value+value,acc['type'],2)
    loop['channels'].append({'sampler':len(loop['samplers']),'target':dict(channel['target'])});loop['samplers'].append({'input':times,'output':output,'interpolation':'LINEAR'})
d['buffers'][0]['byteLength']=len(raw)
# Also keep the self-contained GLB handoff corrected.
j=json.dumps(d,separators=(',',':')).encode();j+=b' '*((-len(j))%4)
source.write_bytes(struct.pack('<III',0x46546c67,2,28+len(j)+len(raw))+struct.pack('<II',len(j),0x4e4f534a)+j+struct.pack('<II',len(raw),0x004e4942)+raw)
for material in d['materials']:
    factor=material.pop('emissiveFactor',[1,1,1]);texture=material.pop('emissiveTexture',None)
    material['pbrMetallicRoughness']={'baseColorFactor':factor+[1],'metallicFactor':0,'roughnessFactor':1}
    if texture:material['pbrMetallicRoughness']['baseColorTexture']=texture
    material.setdefault('extensions',{})['KHR_materials_unlit']={}
d['extensionsUsed']=list(set(d.get('extensionsUsed',[])+['KHR_materials_unlit']))
for index,img in enumerate(d.get('images',[])):
    if 'bufferView' not in img:continue
    view=d['bufferViews'][img.pop('bufferView')];data=raw[view.get('byteOffset',0):view.get('byteOffset',0)+view['byteLength']]
    name='otter-texture.png' if index==0 else 'texture-'+str(index)+'.png'
    (target/name).write_bytes(data);img['uri']='/assets/companion/'+name
used={a['bufferView'] for a in d['accessors'] if 'bufferView' in a}
for a in d['accessors']:
    for part in a.get('sparse',{}).values():
        if isinstance(part,dict) and 'bufferView' in part:used.add(part['bufferView'])
new=bytearray();views=[];mapping={}
for index in sorted(used):
    v=dict(d['bufferViews'][index]);start=v.get('byteOffset',0);mapping[index]=len(views);v['byteOffset']=len(new);new.extend(raw[start:start+v['byteLength']]);new.extend(b'\0'*((-len(new))%4));views.append(v)
for a in d['accessors']:
    if 'bufferView' in a:a['bufferView']=mapping[a['bufferView']]
    for part in a.get('sparse',{}).values():
        if isinstance(part,dict) and 'bufferView' in part:part['bufferView']=mapping[part['bufferView']]
d['bufferViews']=views;d['buffers'][0]['byteLength']=len(new)
j=json.dumps(d,separators=(',',':')).encode();j+=b' '*((-len(j))%4)
out=struct.pack('<III',0x46546c67,2,28+len(j)+len(new))+struct.pack('<II',len(j),0x4e4f534a)+j+struct.pack('<II',len(new),0x004e4942)+new
(target/'otter-data.js').write_text('window.OTTER_GLB="'+base64.b64encode(out).decode()+'";')
report={'originalBytes':len(b),'runtimeBytes':len(out),'nodes':len(d['nodes']),'materials':len(d['materials']),'animations':[a['name'] for a in d['animations']]}
(base/'validation/companion-tablet-20260907/runtime-asset.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))
