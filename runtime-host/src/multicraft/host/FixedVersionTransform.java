package multicraft.host;

import java.lang.classfile.*;
import java.lang.classfile.instruction.InvokeInstruction;
import java.lang.constant.ClassDesc;
import java.lang.constant.MethodTypeDesc;

/** Deliberately small, fail-closed adaptation of the pinned unmodified 26.3 client. */
final class FixedVersionTransform {
    static final String CLIENT_SHA256 = "4508d006323f24fa02876310c192d739af56516eb259000ac50f0909a68c9a2d";
    private static final ClassDesc HOST = ClassDesc.of("multicraft.host.Host");
    private final boolean disableAudio;
    private final String sessionId;
    FixedVersionTransform(boolean disableAudio) { this("static-check",disableAudio); }
    FixedVersionTransform(String sessionId,boolean disableAudio) { this.sessionId=sessionId; this.disableAudio = disableAudio; }

    byte[] apply(String name, byte[] bytes) {
        var model = ClassFile.of().parse(bytes);
        int[] mainHook = {0};
        int[] vulkanHook = {0};
        int[] windowDestroyHook={0},instanceDestroyHook={0};
        int[] initialDisplayHook={0},fullscreenDisplayHook={0},backendWindowHook={0};
        int[] frameWaitHook={0};
        byte[] result = ClassFile.of().transformClass(model, (cb, ce) -> {
            if (!(ce instanceof MethodModel method)) { cb.with(ce); return; }
            String mn = method.methodName().stringValue();
            String descriptor = method.methodType().stringValue();
            if (name.equals("com.mojang.blaze3d.platform.SDLEventHandler") &&
                    (mn.equals("pollEvents") || mn.equals("flushInputEvents"))) {
                cb.withMethodBody(mn, MethodTypeDesc.ofDescriptor(descriptor), method.flags().flagsMask(), code ->
                    code.aload(0).invokestatic(HOST, mn.equals("pollEvents") ? "pollEventsFor" : "flushEventsFor",
                        MethodTypeDesc.ofDescriptor("(Ljava/lang/Object;)V")).return_());
            } else if (name.equals("com.mojang.blaze3d.platform.InputConstants") && mn.equals("isKeyDown")) {
                cb.withMethodBody(mn, MethodTypeDesc.ofDescriptor(descriptor), method.flags().flagsMask(), code ->
                    code.iload(0).invokestatic(HOST, "isKeyDown", MethodTypeDesc.ofDescriptor("(I)Z")).ireturn());
            } else if (name.equals("com.mojang.blaze3d.platform.ClipboardManager") && mn.equals("getClipboard")) {
                cb.withMethodBody(mn, MethodTypeDesc.ofDescriptor(descriptor), method.flags().flagsMask(), code ->
                    code.invokestatic(HOST,"getClipboard",MethodTypeDesc.ofDescriptor("()Ljava/lang/String;")).areturn());
            } else if (name.equals("com.mojang.blaze3d.platform.ClipboardManager") && mn.equals("setClipboard")) {
                cb.withMethodBody(mn, MethodTypeDesc.ofDescriptor(descriptor), method.flags().flagsMask(), code ->
                    code.aload(1).invokestatic(HOST,"setClipboard",MethodTypeDesc.ofDescriptor("(Ljava/lang/String;)V")).return_());
            } else if (name.equals("com.mojang.blaze3d.platform.InputConstants") &&
                    (mn.equals("grabMouse") || mn.equals("releaseMouse"))) {
                // The browser owns each session's relative pointer. SDL has one physical seat.
                cb.withMethodBody(mn, MethodTypeDesc.ofDescriptor(descriptor), method.flags().flagsMask(), CodeBuilder::return_);
            } else if (name.equals("net.minecraft.client.MouseHandler") && mn.equals("resyncMousePosition")) {
                cb.withMethodBody(mn, MethodTypeDesc.ofDescriptor(descriptor), method.flags().flagsMask(), CodeBuilder::return_);
            } else if (name.equals("com.mojang.blaze3d.platform.Window") && mn.equals("isFocused")) {
                cb.withMethodBody(mn, MethodTypeDesc.ofDescriptor(descriptor), method.flags().flagsMask(), code -> code.iconst_1().ireturn());
            } else if (disableAudio && name.equals("net.minecraft.client.sounds.SoundEngine") && mn.equals("loadLibrary")) {
                // A silent feasibility probe is explicitly incomplete M1; never mix contexts silently.
                cb.withMethodBody(mn, MethodTypeDesc.ofDescriptor(descriptor), method.flags().flagsMask(), CodeBuilder::return_);
            } else {
                cb.transformMethod(method, MethodTransform.transformingCode((code, element) -> {
                    if(name.equals("com.mojang.blaze3d.platform.Window") && mn.equals("setTitle") &&
                            element instanceof java.lang.classfile.instruction.LoadInstruction load && load.slot()==1) {
                        code.with(element).invokestatic(HOST,"titleFor",MethodTypeDesc.ofDescriptor("(Ljava/lang/String;)Ljava/lang/String;"));
                        return;
                    }
                    if (element instanceof InvokeInstruction invoke) {
                        String owner = invoke.owner().asInternalName();
                        String called = invoke.name().stringValue();
                        if(name.equals("net.minecraft.client.Minecraft") && owner.equals("net/minecraft/client/FramerateLimiter")
                                && called.equals("limitDisplayFPS") && invoke.type().stringValue().equals("(I)V")) {
                            frameWaitHook[0]++;
                            code.invokestatic(HOST,"hostOwnsFrameWait",invoke.typeSymbol()); return;
                        }
                        if(name.equals("com.mojang.blaze3d.platform.Window") && mn.equals("<init>")
                                && owner.equals("org/lwjgl/sdl/SDLVideo") && called.equals("SDL_GetPrimaryDisplay") && invoke.type().stringValue().equals("()I")) {
                            initialDisplayHook[0]++;
                            code.invokestatic(HOST,"initialDisplay",invoke.typeSymbol()); return;
                        }
                        if(name.equals("com.mojang.blaze3d.platform.Window") && owner.equals("org/lwjgl/sdl/SDLVideo")
                                && called.equals("SDL_SetWindowFullscreen") && invoke.type().stringValue().equals("(JZ)Z")) {
                            fullscreenDisplayHook[0]++;
                            code.invokestatic(HOST,"setNativeFullscreen",invoke.typeSymbol()); return;
                        }
                        if((name.equals("com.mojang.renderpearl.backend.vulkan.VulkanBackend") || name.equals("com.mojang.renderpearl.backend.opengl.GlBackend"))
                                && mn.equals("createWindow") && owner.equals("org/lwjgl/sdl/SDLVideo") && called.equals("SDL_CreateWindow")
                                && invoke.type().stringValue().equals("(Ljava/lang/CharSequence;IIJ)J")) {
                            backendWindowHook[0]++;
                            code.invokestatic(HOST,"createNativeWindow",invoke.typeSymbol()); return;
                        }
                        if(name.equals("com.mojang.blaze3d.platform.Window") && owner.equals("org/lwjgl/sdl/SDLVideo") && called.equals("SDL_DestroyWindow")) {
                            windowDestroyHook[0]++;
                            code.invokestatic(ClassDesc.of("multicraft.host.HostGraphics"),"destroyWindow",invoke.typeSymbol()); return;
                        }
                        if(name.equals("com.mojang.renderpearl.backend.vulkan.VulkanInstance") && owner.equals("org/lwjgl/vulkan/VK12") && called.equals("vkDestroyInstance")) {
                            instanceDestroyHook[0]++;
                            code.invokestatic(ClassDesc.of("multicraft.host.HostGraphics"),"destroyInstance",invoke.typeSymbol()); return;
                        }
                        if(name.equals("com.mojang.blaze3d.platform.NativeLibrariesBootstrap") && owner.equals("org/lwjgl/vulkan/VK") &&
                                called.equals("create") && invoke.type().stringValue().equals("()V")) {
                            vulkanHook[0]++;
                            code.invokestatic(ClassDesc.of("multicraft.host.HostGraphics"),"ensureVulkanCreated",MethodTypeDesc.ofDescriptor("()V")); return;
                        }
                        if (name.equals("net.minecraft.client.main.Main") && owner.equals("net/minecraft/client/Minecraft") &&
                                called.equals("run") && invoke.type().stringValue().equals("()V")) {
                            mainHook[0]++;
                            code.invokestatic(HOST, "registerAndYield", MethodTypeDesc.ofDescriptor("(Ljava/lang/Object;)V"));
                            return;
                        }
                        if (owner.equals("java/lang/System") && called.equals("exit")) {
                            code.invokestatic(HOST, "requestExit", MethodTypeDesc.ofDescriptor("(I)V")); return;
                        }
                        if (owner.equals("org/lwjgl/sdl/SDLInit") && called.equals("SDL_Quit")) {
                            code.invokestatic(HOST, "deferSdlQuit", MethodTypeDesc.ofDescriptor("()V")); return;
                        }
                        if (owner.equals("com/mojang/blaze3d/systems/RenderSystem") && called.equals("unloadTrackedBackendLibrary")) {
                            code.invokestatic(HOST, "deferBackendUnload", MethodTypeDesc.ofDescriptor("()V")); return;
                        }
                        if (owner.equals("org/lwjgl/sdl/SDLKeyboard") && called.equals("SDL_GetModState")) {
                            code.invokestatic(HOST, "modifierState", MethodTypeDesc.ofDescriptor("()S")); return;
                        }
                    }
                    code.with(element);
                }));
            }
        });
        if(!disableAudio && AudioTransform.handles(name)) result=AudioTransform.apply(name,result,sessionId);
        if (name.equals("net.minecraft.client.main.Main") && mainHook[0] != 1)
            throw new IllegalStateException("Expected exactly one original Minecraft.run call, found " + mainHook[0]);
        if(name.equals("com.mojang.blaze3d.platform.NativeLibrariesBootstrap") && vulkanHook[0]!=1)
            throw new IllegalStateException("Expected exactly one original Vulkan loader creation, found "+vulkanHook[0]);
        if(name.equals("com.mojang.blaze3d.platform.Window") && windowDestroyHook[0]!=1)
            throw new IllegalStateException("Expected exactly one original SDL window destruction, found "+windowDestroyHook[0]);
        if(name.equals("com.mojang.blaze3d.platform.Window") && (initialDisplayHook[0]!=1 || fullscreenDisplayHook[0]!=4))
            throw new IllegalStateException("Expected one initial display and four fullscreen calls; found "+initialDisplayHook[0]+"/"+fullscreenDisplayHook[0]);
        if((name.equals("com.mojang.renderpearl.backend.vulkan.VulkanBackend") || name.equals("com.mojang.renderpearl.backend.opengl.GlBackend")) && backendWindowHook[0]!=1)
            throw new IllegalStateException("Expected exactly one original backend SDL window creation, found "+backendWindowHook[0]);
        if(name.equals("com.mojang.renderpearl.backend.vulkan.VulkanInstance") && instanceDestroyHook[0]!=1)
            throw new IllegalStateException("Expected exactly one original Vulkan instance destruction, found "+instanceDestroyHook[0]);
        if(name.equals("net.minecraft.client.Minecraft") && frameWaitHook[0]!=1)
            throw new IllegalStateException("Expected exactly one original blocking display limiter, found "+frameWaitHook[0]);
        var errors = ClassFile.of().verify(result);
        if (!errors.isEmpty()) throw new IllegalStateException("Invalid transformed " + name + ": " + errors);
        return result;
    }
}
