import bpy, math, json, os
from mathutils import Vector, Matrix, Quaternion, Euler

ROOT='D:/Salty Steak'
OUT=ROOT+'/validation/companion-tablet-20260907'
SOURCE='C:/Users/anikh/Documents/Codex/2026-09-06/i-ll-give-you-a-file/outputs/Salty-Steak-Review/model/Otter-Companion.blend'
bpy.ops.wm.open_mainfile(filepath=SOURCE)
rig=bpy.data.objects['Otter_Rig'];skin=bpy.data.objects['Otter_Skin'];scene=bpy.context.scene
rig.animation_data.action=None
for track in rig.animation_data.nla_tracks:track.mute=True
if skin.data.shape_keys.animation_data:
    skin.data.shape_keys.animation_data.action=None
    for track in skin.data.shape_keys.animation_data.nla_tracks:track.mute=True
for bone in rig.pose.bones:bone.matrix_basis=Matrix.Identity(4)
for key in skin.data.shape_keys.key_blocks:key.value=0

def smooth(a,b,x):
    u=max(0,min(1,(x-a)/(b-a)));return u*u*(3-2*u)

# Additional joints leave the supplied rest mesh and UVs unchanged.
bpy.context.view_layer.objects.active=rig
bpy.ops.object.mode_set(mode='EDIT')
specs={}
for side in ['L','R']:
    upper=rig.data.edit_bones['Paw.'+side]
    shoulder=upper.head.copy();fingertip=upper.tail.copy()
    elbow=shoulder.lerp(fingertip,.48);elbow.x+=-.016 if side=='L' else .016
    wrist=shoulder.lerp(fingertip,.88)
    upper.tail=elbow
    fore=rig.data.edit_bones.new('Forepaw.'+side);fore.head=elbow;fore.tail=wrist;fore.parent=upper;fore.use_connect=True
    hand=rig.data.edit_bones.new('Wrist.'+side);hand.head=wrist;hand.tail=fingertip;hand.parent=fore;hand.use_connect=True
    specs[side]=(shoulder,elbow,wrist,fingertip)
bpy.ops.object.mode_set(mode='OBJECT')
for bone in rig.pose.bones:bone.rotation_mode='QUATERNION'
for side in ['L','R']:
    upper=skin.vertex_groups['Paw.'+side]
    fore=skin.vertex_groups.new(name='Forepaw.'+side);hand=skin.vertex_groups.new(name='Wrist.'+side)
    shoulder,elbow,wrist,tip=specs[side]
    for vertex in skin.data.vertices:
        try:weight=upper.weight(vertex.index)
        except RuntimeError:continue
        fore_part=1-smooth(elbow.z-.034,elbow.z+.043,vertex.co.z)
        hand_part=1-smooth(wrist.z-.01,wrist.z+.041,vertex.co.z)
        upper.add([vertex.index],weight*(1-fore_part),'REPLACE')
        fore.add([vertex.index],weight*fore_part*(1-hand_part),'REPLACE')
        hand.add([vertex.index],weight*fore_part*hand_part,'REPLACE')

# Keep the authored tablet and pen mesh; discard subpixel labels/ports and simplify curves.
old=set(scene.objects)
bpy.ops.import_scene.gltf(filepath='C:/Users/anikh/Downloads/apple_ipad_pro.glb')
imported=set(scene.objects)-old
keep=['iPad Pro 2020_Body_0','iPad Pro 2020_screen_0','iPad Pro 2020_bezel_0','camera module_Body_0','camera module_glass_0','Apple Pencil_apple pencil_0','Apple logo_cameraframe and logo_0']
parts=[o for o in imported if o.type=='MESH' and o.name in keep]
for ob in parts:
    world=ob.matrix_world.copy();ob.data=ob.data.copy();ob.data.transform(world);ob.parent=None;ob.matrix_world=Matrix.Identity(4)
    bpy.ops.object.select_all(action='DESELECT');ob.select_set(True);bpy.context.view_layer.objects.active=ob
    if len(ob.data.polygons)>500:
        mod=ob.modifiers.new('Desktop silhouette simplification','DECIMATE');mod.ratio=.35 if 'Pencil' in ob.name else .09
        bpy.ops.object.modifier_apply(modifier=mod.name)
    for poly in ob.data.polygons:poly.use_smooth=True
for ob in imported:
    if ob not in parts:bpy.data.objects.remove(ob,do_unlink=True)

def matte(name,color,emission=False):
    mat=bpy.data.materials.new(name);mat.diffuse_color=(*color,1);mat.use_nodes=True
    nodes=mat.node_tree.nodes;nodes.clear();output=nodes.new('ShaderNodeOutputMaterial')
    shader=nodes.new('ShaderNodeEmission');shader.inputs['Color'].default_value=(*color,1);shader.inputs['Strength'].default_value=1
    mat.node_tree.links.new(shader.outputs[0],output.inputs['Surface']);return mat
materials={'body':matte('Tablet graphite',(0.13,.16,.19)),'bezel':matte('Tablet bezel',(.015,.019,.025)),'screen':matte('Tablet lit screen',(.13,.24,.30)),'pencil':matte('Pencil warm white',(.80,.85,.87)),'glass':matte('Camera glass',(.025,.034,.042)),'logo':matte('Tablet insignia',(.055,.074,.09))}
tablet=bpy.data.objects.new('Tablet_Control',None);scene.collection.objects.link(tablet)
pen=bpy.data.objects.new('Pencil_Control',None);scene.collection.objects.link(pen)
tablet['asset']='Supplied apple_ipad_pro.glb, simplified for desktop scale';pen['asset']='Supplied Apple Pencil mesh'
factor=.40/77.37498474
rot=Matrix.Rotation(math.pi/2,4,'Y')
for ob in parts:
    ispen='Pencil' in ob.name
    if ispen:
        for v in ob.data.vertices:v.co=(v.co-Vector((33.35,.823,16.453226)))*(.19/51.91786)
        ob.parent=pen;ob.name='Tablet_Pencil';mat=materials['pencil']
    else:
        for v in ob.data.vertices:v.co=rot.to_3x3()@((v.co-Vector((.011,.81,38.68)))*factor)
        ob.parent=tablet
        mat=materials['logo'] if 'Apple logo' in ob.name else materials['screen'] if 'screen' in ob.name else materials['bezel'] if 'bezel' in ob.name else materials['glass'] if 'glass' in ob.name else materials['body']
        ob.name='Tablet_'+ob.name
    ob.matrix_parent_inverse=Matrix.Identity(4);ob.matrix_basis=Matrix.Identity(4)
    ob.data.materials.clear();ob.data.materials.append(mat)

# Pen strokes remain distinct from a screen full of text at taskbar size.
def bar(name,center,size,color):
    bpy.ops.mesh.primitive_cube_add(size=1,location=(0,0,0));ob=bpy.context.object;ob.name=name
    ob.scale=size;bpy.ops.object.transform_apply(location=False,rotation=False,scale=True);ob.location=center;ob.parent=tablet;ob.data.materials.append(matte(name+' ink',color));return ob
bar('Tablet_Screen_header',(-.066,-.0055,.11),(.22,.0008,.009),(.36,.62,.68))
for n,length in enumerate([.25,.18,.22]):bar('Tablet_Screen_note_'+str(n),(-.024,-.0055,.06-n*.04),(length,.0008,.006),(.37,.50,.54))

for ob in (tablet,pen):ob.rotation_mode='QUATERNION'
base_tablet=Vector((.118,-.24,.235));base_rotation=Euler((math.radians(-28),0,math.radians(177)),'XYZ').to_quaternion()
rest_l=specs['L'][2];rest_r=specs['R'][2]

def set_limb(side,target):
    shoulder,elbow,wrist,tip=specs[side]
    # Two-joint geometric IK, baked into local rotations for the GLB runtime.
    parent=rig.pose.bones['Spine'].matrix @ rig.data.bones['Spine'].matrix_local.inverted()
    a=parent@shoulder
    direction=target-a;distance=direction.length;direction.normalize()
    l1=(elbow-shoulder).length;l2=(wrist-elbow).length
    distance=max(.02,min(distance,l1+l2-.0005))
    pole=Vector((-.7 if side=='L' else .7,-.4,-.05))
    perpendicular=(pole-direction*direction.dot(pole)).normalized()
    along=(l1*l1-l2*l2+distance*distance)/(2*distance)
    midpoint=a+direction*along+perpendicular*max(0,l1*l1-along*along)**.5
    endpoint=a+direction*distance
    for name,start,end in [('Paw.'+side,a,midpoint),('Forepaw.'+side,midpoint,endpoint)]:
        bone=rig.pose.bones[name]
        rest=rig.data.bones[name].matrix_local.to_quaternion()
        original=rest@Vector((0,1,0));delta=original.rotation_difference((end-start).normalized())
        bone.matrix=Matrix.Translation(start)@(delta@rest).to_matrix().to_4x4()
    bpy.context.view_layer.update()
    return endpoint

def ease(t):t=max(0,min(1,t));return t*t*(3-2*t)
def mix(a,b,t):return Vector(a).lerp(Vector(b),ease(t))

def body_pose(hop=0,crouch=0,nod=0,turn=0,ear=0):
    for b in rig.pose.bones:b.matrix_basis=Matrix.Identity(4)
    root=rig.pose.bones['Root'];root.location=Vector((0,hop,0));root.scale=(1+crouch*.13,1-crouch*.14,1+crouch*.1)
    rig.pose.bones['Spine'].rotation_quaternion=Euler((math.radians(nod*.23),0,math.radians(turn*.3))).to_quaternion()
    rig.pose.bones['Head'].rotation_quaternion=Euler((math.radians(nod),math.radians(turn*.3),math.radians(turn))).to_quaternion()
    rig.pose.bones['Ear.L'].rotation_quaternion=Euler((0,0,math.radians(ear))).to_quaternion()
    rig.pose.bones['Ear.R'].rotation_quaternion=Euler((0,0,math.radians(-ear))).to_quaternion()
    bpy.context.view_layer.update()

def tablet_pose(center,rotation=base_rotation):
    tablet.location=center;tablet.rotation_quaternion=rotation
    tablet.scale=(1,1,1);pen.scale=(1,1,1);bpy.context.view_layer.update()

def fk_paws(amount=1,phase=0,reach=0):
    values={
      'Paw.L':(-9*amount,0,15*amount), 'Forepaw.L':(4*amount,0,5*amount),
      'Paw.R':(-30*amount+34*reach,0,7*amount-15*reach),
      'Forepaw.R':((-15+3*math.sin(phase*2))*amount,0,-4*amount),
      'Wrist.R':(2*amount*math.sin(phase*2),0,3*amount*math.sin(phase*2)),
    }
    for name,angle in values.items():rig.pose.bones[name].rotation_quaternion=Euler(tuple(math.radians(v) for v in angle)).to_quaternion()
    bpy.context.view_layer.update()

grip_ids=sorted(range(len(skin.data.vertices)),key=lambda i:(skin.data.vertices[i].co-Vector((.235,-.206,.315))).length)[:12]
def pencil_in_paw(phase=0):
    evaluated=skin.evaluated_get(bpy.context.evaluated_depsgraph_get())
    grip=sum((evaluated.data.vertices[i].co for i in grip_ids),Vector())/len(grip_ids)+Vector((.005,-.035,.006))
    # The back is visible to us; the screen normal points towards the otter.
    local=tablet.matrix_local.inverted()@grip
    tip=tablet.matrix_local@Vector((max(-.16,min(.16,local.x+.12+.012*math.sin(phase*2))),-.006,max(-.11,min(.11,local.z+.008*math.cos(phase*2)))))
    direction=(grip-tip).normalized()
    pen.location=tip;pen.rotation_quaternion=Vector((0,0,1)).rotation_difference(direction)
    pen.scale=(1.2,1.2,(grip-tip).length/.115)

def work_pose(phase=0):
    body_pose(nod=17+math.sin(phase)*1.1,turn=math.sin(phase)*1.3,ear=math.sin(phase)*1.4)
    fk_paws(1,phase)
    tablet_pose(base_tablet)
    pencil_in_paw(phase)

def entry_pose(t):
    hop=.055*math.sin(math.pi*max(0,min(1,(t-.10)/.26))) if .10<t<.36 else 0
    crouch=.24*math.sin(math.pi*t/.14) if t<.14 else .14*math.sin(math.pi*(t-.36)/.14) if .36<t<.5 else 0
    settle=ease((t-.54)/.46);reach=math.sin(math.pi*t)
    body_pose(hop=hop,crouch=crouch,nod=17*settle,turn=-8*reach,ear=3*reach)
    fk_paws(settle,0,reach)
    behind=Vector((.20,.13,.31));swing=Vector((.34,-.08,.36))
    if t<.24:
        tablet.location=behind;tablet.scale=(.001,)*3;pen.scale=(.001,)*3
    elif t<.60:
        k=(t-.24)/.36;tablet_pose(mix(behind,swing,k),base_rotation.slerp(Euler((-.4,0,2.7)).to_quaternion(),1-ease(k)))
        tablet.scale=(max(.001,ease(k*3)),)*3;pen.scale=(.001,)*3
    else:
        k=(t-.60)/.40;tablet_pose(mix(swing,base_tablet,k),Euler((-.4,0,2.7)).to_quaternion().slerp(base_rotation,ease(k)))
        pencil_in_paw(0);pen.scale*=max(.001,ease((t-.70)/.15))
    if t>=1:work_pose(0)

new_actions={}
animated=[rig,tablet,pen]
def bake(name,end,fn):
    for ob in animated:ob.animation_data_create();ob.animation_data.action=None
    for frame in range(1,end+1):
        scene.frame_set(frame);fn((frame-1)/(end-1))
        for bone in rig.pose.bones:
            for prop in ['location','rotation_quaternion','scale']:bone.keyframe_insert(prop,frame=frame,group=bone.name)
        for ob in (tablet,pen):
            for prop in ['location','rotation_quaternion','scale']:ob.keyframe_insert(prop,frame=frame)
    for ob in animated:
        action=ob.animation_data.action;action.name=name+'_'+ob.name
        new_actions[(name,ob.name)]=action
        tr=ob.animation_data.nla_tracks.new();tr.name=name;tr.strips.new(name,1,action);tr.mute=True;ob.animation_data.action=None
        for layer in action.layers:
            for strip in layer.strips:
                for bag in strip.channelbags:
                    for curve in bag.fcurves:
                        for key in curve.keyframe_points:key.interpolation='LINEAR'

old_clips=['Curious','Greeting','Mildly_annoyed','Sleepy','Tail_tap']
for clip in old_clips:
    old_action=next(t.strips[0].action for t in rig.animation_data.nla_tracks if t.name==clip)
    curves={}
    for layer in old_action.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                for curve in bag.fcurves:
                    if 'rotation_euler' in curve.data_path:
                        name=curve.data_path.split('"')[1];curves[(name,curve.array_index)]=curve
    end=int(old_action.frame_range[1])
    for track in list(rig.animation_data.nla_tracks):
        if track.name==clip:rig.animation_data.nla_tracks.remove(track)
    def pose_old(t,curves=curves,end=end):
        body_pose();tablet.scale=(.001,)*3;pen.scale=(.001,)*3
        for name in set(n for n,i in curves):
            values=[curves[(name,i)].evaluate(1+t*(end-1)) if (name,i) in curves else 0 for i in range(3)]
            rig.pose.bones[name].rotation_quaternion=Euler(values,'XYZ').to_quaternion()
    bake(clip,end,pose_old)

bake('Work_Enter',72,entry_pose)
bake('Work_Loop',121,lambda t:work_pose(t*math.pi*2))
def exit_pose(t):
    k=ease(t);body_pose(nod=17*(1-k),turn=-6*math.sin(math.pi*t),ear=2*math.sin(math.pi*t))
    fk_paws(1-k,0,math.sin(math.pi*t)*.6)
    if t<.55:
        tablet_pose(mix(base_tablet,(.34,-.08,.34),t/.55));pencil_in_paw(0);pen.scale*=max(.001,1-ease(t/.45))
    else:
        u=(t-.55)/.45;tablet_pose(mix((.34,-.08,.34),(.20,.13,.30),u));tablet.scale=(max(.001,1-ease((u-.25)/.75)),)*3;pen.scale=(.001,)*3
    if t>=1:body_pose();tablet.scale=(.001,)*3;pen.scale=(.001,)*3
bake('Work_Exit',54,exit_pose)

body_pose();tablet.location=(.26,.16,.46);tablet.scale=(.001,)*3;pen.scale=(.001,)*3
scene.render.engine='BLENDER_EEVEE';scene.render.fps=30;scene.render.film_transparent=True;scene.view_settings.view_transform='Standard'
scene.render.resolution_x=800;scene.render.resolution_y=800;scene.render.resolution_percentage=100
scene.render.image_settings.file_format='PNG';scene.render.image_settings.color_mode='RGBA'
camera=scene.camera;center=Vector((.0,-.01,.5));camera.location=center+Vector((.43,-2.7,.24));camera.rotation_euler=(center-camera.location).to_track_quat('-Z','Y').to_euler();camera.data.ortho_scale=1.27
rig['working_motion']='Work_Enter 2.4s, Work_Loop 4s, Work_Exit 1.8s. New elbow and wrist joints. Driven by real generation state.'
bpy.ops.wm.save_as_mainfile(filepath=OUT+'/Otter-Tablet-Companion.blend')
for name,frame in [('Work_Enter',34),('Work_Enter',60),('Work_Loop',30),('Work_Loop',90),('Work_Exit',35)]:
    for ob in animated:ob.animation_data.action=new_actions[(name,ob.name)]
    scene.frame_set(frame);scene.render.filepath=OUT+'/'+name+'-'+str(frame)+'.png';bpy.ops.render.render(write_still=True)
for ob in animated:ob.animation_data.action=new_actions[('Work_Loop',ob.name)]
scene.frame_set(30);camera.location=(1.45,-1.1,.85);camera.rotation_euler=(Vector((.12,-.12,.46))-camera.location).to_track_quat('-Z','Y').to_euler();scene.render.filepath=OUT+'/Work_Loop-side.png';bpy.ops.render.render(write_still=True)
for ob in animated:ob.animation_data.action=None
body_pose();tablet.scale=(.001,)*3;pen.scale=(.001,)*3
bpy.ops.object.select_all(action='DESELECT')
for ob in [rig,skin,tablet,pen]+[o for o in scene.objects if o.name.startswith('Tablet_')]:ob.select_set(True)
bpy.ops.export_scene.gltf(filepath=OUT+'/Otter-Tablet-Companion.glb',use_selection=True,export_format='GLB',export_animations=True,export_animation_mode='NLA_TRACKS',export_force_sampling=True,export_morph=True,export_skins=True)
meshes=[o for o in scene.objects if o.type=='MESH']
json.dump({'bones':len(rig.data.bones),'meshes':[(o.name,len(o.data.polygons),sum(len(p.vertices)-2 for p in o.data.polygons)) for o in meshes],'clips':['Work_Enter','Work_Loop','Work_Exit'],'triangles':sum(sum(len(p.vertices)-2 for p in o.data.polygons) for o in meshes)},open(OUT+'/motion-model-report.json','w'),indent=2)
print('TABLET_MOTION_READY',flush=True)
