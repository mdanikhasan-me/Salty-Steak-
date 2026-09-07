import bpy
OUT='D:/Salty Steak/validation/companion-tablet-20260907'
bpy.ops.wm.open_mainfile(filepath=OUT+'/Otter-Tablet-Companion.blend')
bpy.ops.object.select_all(action='DESELECT')
for ob in bpy.context.scene.objects:
    if ob.type in ('MESH','ARMATURE') or ob.name in ('Tablet_Control','Pencil_Control'):ob.select_set(True)
props=bpy.ops.export_scene.gltf.get_rna_type().properties.keys()
settings={'export_optimize_animation_size':False} if 'export_optimize_animation_size' in props else {}
print('FULL_CHANNELS',settings)
bpy.ops.export_scene.gltf(filepath=OUT+'/Otter-Tablet-Companion.glb',use_selection=True,export_format='GLB',export_animations=True,export_animation_mode='NLA_TRACKS',export_force_sampling=True,export_morph=True,export_skins=True,**settings)
