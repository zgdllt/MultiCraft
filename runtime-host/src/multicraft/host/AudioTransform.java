package multicraft.host;

import java.lang.classfile.*;
import java.lang.classfile.instruction.*;
import java.lang.constant.*;
import java.util.Set;

/** Fixed-version audio adaptation; no physical default device or global current context. */
final class AudioTransform {
    private static final ClassDesc AUDIO = ClassDesc.of("multicraft.host.HostAudio");
    private static final Set<String> CLASSES = Set.of("com.mojang.blaze3d.audio.Library",
        "com.mojang.blaze3d.audio.Channel", "com.mojang.blaze3d.audio.DeviceList",
        "com.mojang.blaze3d.audio.Listener", "com.mojang.blaze3d.audio.OpenAlUtil",
        "com.mojang.blaze3d.audio.SoundBuffer", "com.mojang.blaze3d.audio.CallbackDeviceTracker");
    static boolean handles(String name) { return CLASSES.contains(name); }

    static byte[] apply(String name, byte[] bytes, String sessionId) {
        return ClassFile.of().transformClass(ClassFile.of().parse(bytes), (cb, ce) -> {
            if (!(ce instanceof MethodModel method)) { cb.with(ce); return; }
            String mn=method.methodName().stringValue(), descriptor=method.methodType().stringValue();
            var type=MethodTypeDesc.ofDescriptor(descriptor);
            if(name.endsWith(".Library") && mn.equals("openDeviceOrFallback")) {
                cb.withMethodBody(mn,type,method.flags().flagsMask(), c -> c.ldc(sessionId)
                    .invokestatic(AUDIO,"openLoopback",MethodTypeDesc.ofDescriptor("(Ljava/lang/String;)J")).lreturn());
            } else if(name.endsWith(".Library") && mn.equals("createDeviceTracker")) {
                ClassDesc tracker=ClassDesc.of("com.mojang.blaze3d.audio.DeviceTracker");
                cb.withMethodBody(mn,type,method.flags().flagsMask(), c -> c.ldc(tracker)
                    .invokestatic(AUDIO,"fixedDeviceTracker",MethodTypeDesc.ofDescriptor("(Ljava/lang/Class;)Ljava/lang/Object;"))
                    .checkcast(tracker).areturn());
            } else if(name.endsWith(".DeviceList") && mn.equals("query")) {
                ClassDesc list=ClassDesc.of("com.mojang.blaze3d.audio.DeviceList");
                cb.withMethodBody(mn,type,method.flags().flagsMask(), c -> c.ldc(list)
                    .invokestatic(AUDIO,"fixedDeviceList",MethodTypeDesc.ofDescriptor("(Ljava/lang/Class;)Ljava/lang/Object;"))
                    .checkcast(list).areturn());
            } else if(name.endsWith(".Library") && mn.equals("isCurrentDeviceDisconnected")) {
                cb.withMethodBody(mn,type,method.flags().flagsMask(),c->c.iconst_0().ireturn());
            } else if(name.endsWith(".CallbackDeviceTracker") && mn.equals("isSupported")) {
                cb.withMethodBody(mn,type,method.flags().flagsMask(),c->c.iconst_0().ireturn());
            } else {
                boolean resource=name.endsWith(".Channel") || (name.endsWith(".SoundBuffer") &&
                    (mn.equals("getAlBuffer") || mn.equals("discardAlBuffer") || mn.equals("releaseAlBuffer")));
                boolean direct=method.code().stream().flatMap(code->code.elementStream()).anyMatch(e ->
                    e instanceof InvokeInstruction i && i.owner().asInternalName().startsWith("org/lwjgl/openal/"));
                boolean constructor=mn.equals("<init>"), isStatic=(method.flags().flagsMask() & 8)!=0;
                boolean scoped=!constructor && !mn.equals("<clinit>") && (resource || direct);
                cb.transformMethod(method, MethodTransform.transformingCode(new CodeTransform() {
                    Label start,end,handler;
                    int thrown;
                    @Override public void atStart(CodeBuilder c) {
                        if(!scoped) return;
                        start=c.newLabel(); end=c.newLabel(); handler=c.newLabel();
                        thrown=c.allocateLocal(TypeKind.REFERENCE);
                        c.ldc(sessionId);
                        if(resource && !isStatic) c.aload(0); else c.aconst_null();
                        c.iconst_0(); // Reserved immutable per-object epoch; host records first valid use.
                        c.invokestatic(AUDIO,"enter",MethodTypeDesc.ofDescriptor("(Ljava/lang/String;Ljava/lang/Object;I)Z"));
                        Label accepted=c.newLabel(); c.ifne(accepted);
                        defaultReturn(c,type.returnType(),mn);
                        c.labelBinding(accepted).labelBinding(start);
                    }
                    @Override public void accept(CodeBuilder c,CodeElement element) {
                        if(element instanceof ReturnInstruction) {
                            if(constructor && name.endsWith(".Channel")) c.ldc(sessionId).aload(0)
                                .invokestatic(AUDIO,"tag",MethodTypeDesc.ofDescriptor("(Ljava/lang/String;Ljava/lang/Object;)V"));
                            if(scoped) exit(c,sessionId);
                        }
                        if(element instanceof InvokeInstruction invoke && invoke.owner().asInternalName().startsWith("org/lwjgl/openal/")) {
                            String owner=invoke.owner().asInternalName(), called=invoke.name().stringValue(), desc=invoke.type().stringValue();
                            String replacement=null, replacementDesc=null;
                            if(owner.equals("org/lwjgl/openal/ALC") && called.equals("createCapabilities")) {
                                replacement="createAlcCapabilities"; replacementDesc="(JLjava/lang/String;)Lorg/lwjgl/openal/ALCCapabilities;";
                            } else if(owner.equals("org/lwjgl/openal/AL") && called.equals("createCapabilities")) {
                                replacement="createAlCapabilities"; replacementDesc="(Lorg/lwjgl/openal/ALCCapabilities;Ljava/lang/String;)Lorg/lwjgl/openal/ALCapabilities;";
                            } else if(owner.equals("org/lwjgl/openal/ALC10")) {
                                switch(called) {
                                    case "alcCreateContext" -> { replacement="createContext"; replacementDesc="(JLjava/nio/IntBuffer;Ljava/lang/String;)J"; }
                                    case "alcMakeContextCurrent" -> { replacement="makeContextCurrent"; replacementDesc="(JLjava/lang/String;)Z"; }
                                    case "alcDestroyContext" -> { replacement="destroyContext"; replacementDesc="(JLjava/lang/String;)V"; }
                                    case "alcCloseDevice" -> { replacement="closeDevice"; replacementDesc="(JLjava/lang/String;)Z"; }
                                }
                            }
                            if(replacement!=null) {
                                c.ldc(sessionId).invokestatic(AUDIO,replacement,MethodTypeDesc.ofDescriptor(replacementDesc)); return;
                            }
                            // Rebind immediately before every original AL call, including IO cleanup.
                            c.ldc(sessionId).invokestatic(AUDIO,"rebind",MethodTypeDesc.ofDescriptor("(Ljava/lang/String;)V"));
                        }
                        c.with(element);
                    }
                    @Override public void atEnd(CodeBuilder c) {
                        if(!scoped) return;
                        c.labelBinding(end).labelBinding(handler).astore(thrown);
                        exit(c,sessionId); c.aload(thrown).athrow();
                        c.exceptionCatchAll(start,end,handler);
                    }
                }));
            }
        });
    }
    private static void exit(CodeBuilder c,String id) {
        c.ldc(id).invokestatic(AUDIO,"exit",MethodTypeDesc.ofDescriptor("(Ljava/lang/String;)V"));
    }
    private static void defaultReturn(CodeBuilder c,ClassDesc type,String method) {
        switch(type.descriptorString()) {
            case "V" -> c.return_();
            case "Z" -> { if(method.equals("stopped")) c.iconst_1(); else c.iconst_0(); c.ireturn(); }
            case "I","B","C","S" -> { if(method.equals("getState")) c.ldc(4116); else c.iconst_0(); c.ireturn(); }
            case "J" -> c.lconst_0().lreturn();
            case "F" -> c.fconst_0().freturn();
            case "D" -> c.dconst_0().dreturn();
            case "Ljava/util/OptionalInt;" -> c.invokestatic(ClassDesc.of("java.util.OptionalInt"),"empty",MethodTypeDesc.ofDescriptor("()Ljava/util/OptionalInt;")).areturn();
            default -> c.aconst_null().areturn();
        }
    }
}
