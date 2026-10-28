package multicraft.host;

import java.net.URL;
import java.nio.file.*;
import java.security.MessageDigest;
import java.util.HexFormat;
import java.util.jar.JarFile;

/** Static bytecode checks only: does not construct clients, create windows or connect. */
public final class TransformCheck {
    public static void main(String[] args) throws Exception {
        Path client = Path.of(args[0]);
        try (var input = Files.newInputStream(client)) {
            String hash = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(input.readAllBytes()));
            if (!hash.equals(FixedVersionTransform.CLIENT_SHA256)) throw new IllegalArgumentException("Client hash mismatch");
        }
        FixedVersionTransform transform = new FixedVersionTransform(args.length<2 || !args[1].equals("loopback-pcm"));
        String[] targets = {"net.minecraft.client.main.Main", "net.minecraft.client.Minecraft", "net.minecraft.util.Util",
            "com.mojang.blaze3d.platform.SDLEventHandler", "com.mojang.blaze3d.platform.InputConstants",
            "com.mojang.blaze3d.platform.NativeLibrariesBootstrap",
            "com.mojang.renderpearl.backend.vulkan.VulkanInstance",
            "com.mojang.renderpearl.backend.vulkan.VulkanBackend", "com.mojang.renderpearl.backend.opengl.GlBackend",
            "com.mojang.blaze3d.platform.ClipboardManager", "net.minecraft.client.input.InputQuirks",
            "com.mojang.blaze3d.platform.Window", "net.minecraft.client.MouseHandler", "net.minecraft.client.sounds.SoundEngine",
            "com.mojang.blaze3d.audio.Library", "com.mojang.blaze3d.audio.Channel", "com.mojang.blaze3d.audio.DeviceList",
            "com.mojang.blaze3d.audio.Listener", "com.mojang.blaze3d.audio.OpenAlUtil", "com.mojang.blaze3d.audio.SoundBuffer",
            "com.mojang.blaze3d.audio.CallbackDeviceTracker"};
        try (JarFile jar = new JarFile(client.toFile())) {
            for (String name : targets) {
                byte[] original = jar.getInputStream(jar.getJarEntry(name.replace('.','/') + ".class")).readAllBytes();
                byte[] changed = transform.apply(name, original);
                if (changed.length == 0) throw new AssertionError("Empty transformed class " + name);
                System.out.println("verified " + name);
            }
        }
        System.out.println("STATIC CHECK ONLY: no native window, no gameplay, full M1 incomplete");
    }
}
