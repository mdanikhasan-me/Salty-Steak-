import * as THREE from 'three';
import {GLTFLoader} from 'three/examples/jsm/loaders/GLTFLoader.js';
import {clone} from 'three/examples/jsm/utils/SkeletonUtils.js';

let assetPromise;
function asset() {
  if(!assetPromise) assetPromise=new Promise((resolve,reject)=>{
    const data=Uint8Array.from(atob(window.OTTER_GLB),c=>c.charCodeAt(0));
    new GLTFLoader().parse(data.buffer,'',resolve,reject);
  });
  return assetPromise;
}
window.otterInstances=[];
window.mountOtter=async function(host){
  if(host.otter)return host.otter;
  const model=await asset();if(!host.isConnected)return;
  const renderer=new THREE.WebGLRenderer({alpha:true,antialias:true,preserveDrawingBuffer:true,powerPreference:'low-power'});
  renderer.setPixelRatio(Math.min(1.5,devicePixelRatio));renderer.setClearColor(0,0);renderer.outputColorSpace=THREE.SRGBColorSpace;
  host.appendChild(renderer.domElement);
  const scene=new THREE.Scene(),pet=clone(model.scene);scene.add(pet);
  const body=pet.getObjectByName('Otter_Skin')||pet;
  const bounds=new THREE.Box3().setFromObject(body),center=bounds.getCenter(new THREE.Vector3());
  const camera=new THREE.PerspectiveCamera(27,1,.001,100);
  camera.position.copy(center).add(new THREE.Vector3(.19,.075,2.85));camera.lookAt(center);
  const mixer=new THREE.AnimationMixer(pet),clips=new Map(model.animations.map(c=>[c.name,c]));
  const propRoots=['Tablet_Control','Pencil_Control'].map(name=>pet.getObjectByName(name)).filter(Boolean);
  let raf=0,last=0,renderCount=0,playing=false,working=false,phase='idle',active=null,disposed=false,yaw=0;
  const retired=new Set();
  const reduced=()=>document.documentElement.classList.contains('reduce-motion')||document.body.classList.contains('reduced')||matchMedia('(prefers-reduced-motion: reduce)').matches;
  const props=show=>propRoots.forEach(o=>o.visible=show);
  const draw=()=>{
    if(disposed||!host.isConnected)return;
    const width=host.clientWidth,height=host.clientHeight;if(!width||!height)return;
    if(renderer.domElement.width!==Math.round(width*renderer.getPixelRatio())||renderer.domElement.height!==Math.round(height*renderer.getPixelRatio())){
      renderer.setSize(width,height,false);camera.aspect=width/height;camera.updateProjectionMatrix();
    }
    pet.rotation.y=yaw;renderer.render(scene,camera);renderCount++;
  };
  const rest=()=>{
    playing=false;phase='idle';cancelAnimationFrame(raf);mixer.stopAllAction();active=null;retired.clear();props(false);draw();
    host.dispatchEvent(new CustomEvent('otterrest'));
  };
  const tick=time=>{
    if(!playing||disposed)return;
    const elapsed=(time-last)/1000;
    if(elapsed<1/60){raf=requestAnimationFrame(tick);return;}
    last=time;mixer.update(Math.min(.05,elapsed));
    for(const action of retired){if(action.getEffectiveWeight()<.001){action.stop();retired.delete(action);}}
    draw();if(playing)raf=requestAnimationFrame(tick);
  };
  const start=(name,loop=false,fade=.12)=>{
    const clip=clips.get(name);if(!clip)return false;
    const previous=active;active=mixer.clipAction(clip);
    active.reset().setLoop(loop?THREE.LoopRepeat:THREE.LoopOnce,loop?Infinity:1);
    active.clampWhenFinished=true;active.enabled=true;active.setEffectiveTimeScale(1);active.setEffectiveWeight(1);active.play();
    if(previous&&previous!==active&&fade>0){previous.fadeOut(fade);active.fadeIn(fade);retired.add(previous);}
    phase=name;props(name.startsWith('Work_'));
    if(!playing){playing=true;last=performance.now();raf=requestAnimationFrame(tick);}
    return true;
  };
  const setWorking=value=>{
    const next=Boolean(value);working=next;
    if(reduced()){if(phase!=='idle'||playing)rest();return;}
    if(next){
      if(phase==='Work_Enter'||phase==='Work_Loop')return;
      start('Work_Enter',false,.16);
    }else if(phase==='Work_Enter'&&active){
      phase='Work_Reverse';active.paused=false;active.setEffectiveTimeScale(-1);active.clampWhenFinished=true;
    }else if(phase==='Work_Loop')start('Work_Exit',false,.18);
  };
  mixer.addEventListener('finished',event=>{
    if(event.action!==active)return;
    if(phase==='Work_Enter'&&working)start('Work_Loop',true,.08);
    else if(phase==='Work_Exit'&&working)start('Work_Enter',false,.15);
    else rest();
  });
  const play=name=>{
    if(working||reduced()||!clips.has(name))return;
    rest();start(name,false,0);
  };
  const resize=new ResizeObserver(draw);resize.observe(host);
  const dispose=()=>{
    disposed=true;playing=false;cancelAnimationFrame(raf);resize.disconnect();mixer.stopAllAction();renderer.dispose();renderer.forceContextLoss();renderer.domElement.remove();
    host.otter=null;window.otterInstances=window.otterInstances.filter(o=>o.host!==host);
  };
  const instance={host,renderer,scene,pet,camera,mixer,play,setWorking,reset:()=>{working=false;rest();},rotate:value=>{yaw=value;draw();},draw,dispose,
    stats:()=>({phase,working,playing,renderCount,triangles:renderer.info.render.triangles,calls:renderer.info.render.calls,clips:model.animations.map(c=>({name:c.name,duration:c.duration}))})};
  host.otter=instance;window.otterInstances.push(instance);
  host.addEventListener('click',()=>{
    if(playing||working)return;
    const names=['Curious','Greeting','Tail_tap','Sleepy','Mildly_annoyed'],index=Number(host.dataset.reaction||0);
    play(names[index%names.length]);host.dataset.reaction=String(index+1);
  });
  props(false);draw();return instance;
};
window.mountOtters=()=>document.querySelectorAll('[data-otter]').forEach(host=>{if(!host.otter&&!host.dataset.mounting){host.dataset.mounting='1';window.mountOtter(host).catch(e=>{host.dataset.error=e.message;});}});
window.reactOtter=name=>document.querySelector('[data-otter]')?.otter?.play(name);
window.stopOtters=()=>window.otterInstances.forEach(o=>o.reset());
window.otterAsset=asset;
