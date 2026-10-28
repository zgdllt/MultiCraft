package multicraft.host;

import com.google.gson.*;
import org.lwjgl.*;
import org.lwjgl.system.MemoryStack;
import org.lwjgl.vulkan.*;
import java.lang.reflect.Method;
import java.net.URL;
import java.nio.file.*;
import java.security.MessageDigest;
import java.util.*;

/** Standalone loader/device probe: no Minecraft instance, SDL window or protocol connection. */
public final class GraphicsCheck {
    public static void main(String[] args) throws Exception {
        JsonObject config=JsonParser.parseString(Files.readString(Path.of(args[0]))).getAsJsonObject();
        Path client=Path.of(config.get("clientJar").getAsString());
        try(var input=Files.newInputStream(client)) {
            if(!HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(input.readAllBytes())).equals(FixedVersionTransform.CLIENT_SHA256))
                throw new IllegalArgumentException("Client hash mismatch");
        }
        List<URL> urls=new ArrayList<>(); urls.add(client.toUri().toURL());
        for(JsonElement library:config.getAsJsonArray("libraries")) urls.add(Path.of(library.getAsString()).toUri().toURL());
        Object firstProvider=null;
        for(String id:List.of("graphics-check-A","graphics-check-B")) {
            try(RealmLoader loader=new RealmLoader(id,urls.toArray(URL[]::new),true)) {
                Thread.currentThread().setContextClassLoader(loader);
                Class<?> bootstrap=loader.loadClass("com.mojang.blaze3d.platform.NativeLibrariesBootstrap");
                Method probe=bootstrap.getDeclaredMethod("tryLoadingVulkan"); probe.setAccessible(true);
                if(!Boolean.TRUE.equals(probe.invoke(null))) throw new AssertionError("Realm Vulkan loader rejected: "+id);
                Object provider=VK.getFunctionProvider();
                if(firstProvider!=null && firstProvider!=provider) throw new AssertionError("Realms replaced the process Vulkan loader");
                firstProvider=provider;
                System.out.println("loader accepted "+id+"; shared provider identity="+System.identityHashCode(provider));
            }
        }
        try(MemoryStack stack=MemoryStack.stackPush()) {
            VkApplicationInfo app=VkApplicationInfo.calloc(stack).sType$Default().pApplicationName(stack.UTF8("MultiCraft GraphicsCheck"))
                .apiVersion(VK12.VK_API_VERSION_1_2);
            VkInstanceCreateInfo create=VkInstanceCreateInfo.calloc(stack).sType$Default().pApplicationInfo(app);
            PointerBuffer pointer=stack.mallocPointer(1);
            if(VK12.vkCreateInstance(create,null,pointer)!=VK12.VK_SUCCESS) throw new AssertionError("Cannot create standalone Vulkan instance");
            VkInstance instance=new VkInstance(pointer.get(0),create);
            boolean nvidia=false;
            try {
                var count=stack.mallocInt(1);
                if(VK12.vkEnumeratePhysicalDevices(instance,count,null)!=VK12.VK_SUCCESS) throw new AssertionError("Cannot enumerate physical devices");
                PointerBuffer devices=stack.mallocPointer(count.get(0));
                if(VK12.vkEnumeratePhysicalDevices(instance,count,devices)!=VK12.VK_SUCCESS) throw new AssertionError("Cannot enumerate physical devices");
                for(int i=0;i<count.get(0);i++) {
                    VkPhysicalDeviceProperties info=VkPhysicalDeviceProperties.calloc(stack);
                    VK12.vkGetPhysicalDeviceProperties(new VkPhysicalDevice(devices.get(i),instance),info);
                    System.out.println("native physical device="+info.deviceNameString()+"; vendorId=0x"+Integer.toHexString(info.vendorID())+"; type="+info.deviceType());
                    if(info.vendorID()==0x10de && info.deviceType()==VK12.VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU) nvidia=true;
                }
            } finally { VK12.vkDestroyInstance(instance,null); }
            if(!nvidia) throw new AssertionError("This test host did not expose its required NVIDIA discrete GPU");
        }
        System.out.println("NATIVE LOADER/DEVICE CHECK ONLY: both realm bootstraps share one valid Vulkan loader; NVIDIA device enumerated. No game renderer, window, protocol session or FPS tested.");
    }
}
