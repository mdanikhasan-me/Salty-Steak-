
(async () => {
  const host=document.querySelector('#companion');
  const pet=await mountOtter(host);
  const notifyBaseline=()=>{
    pet.draw();
    const source=pet.renderer.domElement, canvas=document.createElement('canvas');
    canvas.width=source.width;canvas.height=source.height;
    const ctx=canvas.getContext('2d');ctx.drawImage(source,0,0);
    const pixels=ctx.getImageData(0,0,canvas.width,canvas.height).data;
    let baseline=0;
    for(let y=canvas.height-1;y>=0&&!baseline;y--)for(let x=0;x<canvas.width;x++)if(pixels[(y*canvas.width+x)*4+3]>90){baseline=(y+1)/canvas.height;break;}
    window.chrome?.webview?.postMessage({baseline:baseline||.88});
  };
  window.chrome?.webview?.addEventListener('message',event=>{
    if(event.data?.type==='companion_preferences'){
      document.documentElement.classList.toggle('reduce-motion',Boolean(event.data.settings.reducedMotion));
      if(event.data.settings.reducedMotion)pet.reset();
    }
  });
  notifyBaseline();
  window.addEventListener('contextmenu',event=>event.preventDefault());
})();
