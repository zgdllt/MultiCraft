package multicraft.host;

import java.io.IOException;
import java.net.URL;
import java.net.URLClassLoader;
import java.util.Enumeration;

/** All game and renderer statics are realm-local; JNI bindings have one parent owner. */
final class RealmLoader extends URLClassLoader {
    final String sessionId;
    private final FixedVersionTransform transformer;
    static { registerAsParallelCapable(); }
    RealmLoader(String id, URL[] urls, boolean disableAudio) {
        super("MultiCraft-" + id, urls, Host.class.getClassLoader());
        sessionId = id;
        transformer = new FixedVersionTransform(id,disableAudio);
    }
    private static boolean parentOwned(String name) {
        return name.startsWith("java.") || name.startsWith("javax.") || name.startsWith("jdk.") ||
               name.startsWith("sun.") || name.startsWith("com.sun.") || name.startsWith("org.w3c.") ||
               name.startsWith("org.xml.") || name.startsWith("org.lwjgl.") ||
               name.startsWith("com.mojang.jtracy.") || name.startsWith("multicraft.host.");
    }
    @Override protected Class<?> loadClass(String name, boolean resolve) throws ClassNotFoundException {
        synchronized (getClassLoadingLock(name)) {
            Class<?> result = findLoadedClass(name);
            if (result == null) {
                if (parentOwned(name)) result = getParent().loadClass(name);
                else result = findClass(name); // No silent parent fallback for mutable game state.
            }
            if (resolve) resolveClass(result);
            return result;
        }
    }
    @Override protected Class<?> findClass(String name) throws ClassNotFoundException {
        URL resource = findResource(name.replace('.', '/') + ".class");
        if (resource == null) throw new ClassNotFoundException(name);
        try (var input = resource.openStream()) {
            byte[] bytes = input.readAllBytes();
            if (needsTransform(name)) bytes = transformer.apply(name, bytes);
            return defineClass(name, bytes, 0, bytes.length);
        } catch (IOException e) { throw new ClassNotFoundException(name, e); }
    }
    private static boolean needsTransform(String name) {
        if(AudioTransform.handles(name)) return true;
        return switch (name) {
            case "net.minecraft.client.main.Main", "net.minecraft.client.Minecraft", "net.minecraft.util.Util",
                 "com.mojang.blaze3d.platform.SDLEventHandler", "com.mojang.blaze3d.platform.InputConstants",
                 "com.mojang.blaze3d.platform.Window", "net.minecraft.client.MouseHandler",
                 "com.mojang.blaze3d.platform.NativeLibrariesBootstrap",
                 "com.mojang.renderpearl.backend.vulkan.VulkanInstance",
                 "com.mojang.renderpearl.backend.vulkan.VulkanBackend", "com.mojang.renderpearl.backend.opengl.GlBackend",
                 "com.mojang.blaze3d.platform.ClipboardManager", "net.minecraft.client.input.InputQuirks",
                 "net.minecraft.client.sounds.SoundEngine" -> true;
            default -> false;
        };
    }
    @Override public URL getResource(String name) {
        URL own = findResource(name);
        return own == null ? getParent().getResource(name) : own;
    }
    @Override public Enumeration<URL> getResources(String name) throws IOException {
        // Service providers must match the realm's API classes, never a parent duplicate.
        return findResources(name);
    }
}
