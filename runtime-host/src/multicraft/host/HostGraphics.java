package multicraft.host;

import org.lwjgl.system.FunctionProvider;
import org.lwjgl.vulkan.VK;
import org.lwjgl.vulkan.VK12;
import org.lwjgl.vulkan.VkInstance;
import org.lwjgl.vulkan.VkAllocationCallbacks;
import org.lwjgl.sdl.SDLVideo;
import java.util.*;
import java.time.Instant;
import com.google.gson.Gson;

/** The Vulkan loader is process-owned; devices and surfaces remain realm-owned. */
public final class HostGraphics {
    private static FunctionProvider provider;
    private static Thread owner;
    private static String teardownMode="standard";
    private static final Set<Long> deferredWindows=new LinkedHashSet<>();
    private static final Map<Long,VkInstance> deferredInstances=new LinkedHashMap<>();
    private record SessionNative(long window,long instance) {}
    private static final Map<String,SessionNative> sessionObjects=new LinkedHashMap<>();
    private static final Set<Long> destroyedWindows=new HashSet<>(),destroyedInstances=new HashSet<>();
    private static long deferredWindowCount,deferredInstanceCount,releasedWindowCount,releasedInstanceCount;
    private static final Gson EVENTS=new Gson();
    private HostGraphics() {}
    public static synchronized void ensureVulkanCreated() {
        FunctionProvider current;
        try { current=VK.getFunctionProvider(); }
        catch(IllegalStateException notCreated) { current=null; }
        if(provider!=null && current!=provider) throw new IllegalStateException("Process Vulkan loader was unexpectedly replaced or destroyed");
        if(current==null) { VK.create(); current=VK.getFunctionProvider(); }
        if(current==null || current.getFunctionAddress("vkGetInstanceProcAddr")==0 || VK.getInstanceVersionSupported()==0)
            throw new IllegalStateException("Vulkan loader did not initialize completely");
        provider=current;
    }
    public static void configureTeardown(String mode,Thread ownerThread) {
        if(!Set.of("standard","defer-window","defer-instance","defer-native").contains(mode)) throw new IllegalArgumentException("Unknown teardownMode");
        if(owner!=null) throw new IllegalStateException("Graphics teardown is already configured");
        owner=Objects.requireNonNull(ownerThread); teardownMode=mode;
    }
    private static void requireOwner() {
        if(owner!=null && Thread.currentThread()!=owner) throw new IllegalStateException("Graphics teardown outside owner thread");
    }
    public static void registerSession(String session,long window,long instance) {
        requireOwner();
        if(window==0 || sessionObjects.putIfAbsent(session,new SessionNative(window,instance))!=null)
            throw new IllegalStateException("Invalid or duplicate native session registration");
        // Native handle addresses can be reused after an earlier failed backend probe.
        destroyedWindows.remove(window); if(instance!=0) destroyedInstances.remove(instance);
    }
    public static boolean sessionReleased(String session) {
        requireOwner(); SessionNative objects=sessionObjects.get(session);
        return objects!=null && destroyedWindows.contains(objects.window())
            && (objects.instance()==0 || destroyedInstances.contains(objects.instance()));
    }
    public static boolean deferredEmpty() { requireOwner(); return deferredWindows.isEmpty() && deferredInstances.isEmpty(); }
    /** Defer only this exact native call; all preceding realm cleanup still runs. */
    public static void destroyWindow(long window) {
        requireOwner();
        if(teardownMode.equals("defer-window") || teardownMode.equals("defer-native")) {
            boolean added=deferredWindows.add(window); if(added) deferredWindowCount++;
            event(added ? "enqueue-window" : "duplicate-window",window);
        } else {
            event("destroy-window-begin",window); SDLVideo.SDL_DestroyWindow(window); destroyedWindows.add(window); event("destroy-window-complete",window);
        }
    }
    /** Debug messenger, device and surface cleanup still run before this call. */
    public static void destroyInstance(VkInstance instance,VkAllocationCallbacks allocator) {
        requireOwner();
        if(teardownMode.equals("defer-instance") || teardownMode.equals("defer-native")) {
            if(allocator!=null) throw new IllegalStateException("Deferred instances require the pinned null allocator");
            boolean added=deferredInstances.putIfAbsent(instance.address(),instance)==null; if(added) deferredInstanceCount++;
            event(added ? "enqueue-instance" : "duplicate-instance",instance.address());
        } else {
            event("destroy-instance-begin",instance.address()); VK12.vkDestroyInstance(instance,allocator); destroyedInstances.add(instance.address()); event("destroy-instance-complete",instance.address());
        }
    }
    /** Called only after the host has closed every session, before process SDL_Quit. */
    public static void releaseDeferred() {
        requireOwner();
        // Combined experiment keeps every SDL window alive until all Vulkan
        // instances are gone. Single-target modes retain their original order.
        if(teardownMode.equals("defer-native")) releaseInstances();
        releaseWindows();
        if(!teardownMode.equals("defer-native")) releaseInstances();
    }
    private static void releaseWindows() {
        for(long window:new ArrayList<>(deferredWindows)) {
            event("release-window-begin",window);
            SDLVideo.SDL_DestroyWindow(window); destroyedWindows.add(window); deferredWindows.remove(window); releasedWindowCount++;
            event("release-window-complete",window);
        }
    }
    private static void releaseInstances() {
        for(var entry:new ArrayList<>(deferredInstances.entrySet())) {
            event("release-instance-begin",entry.getKey());
            VK12.vkDestroyInstance(entry.getValue(),null); destroyedInstances.add(entry.getKey()); deferredInstances.remove(entry.getKey()); releasedInstanceCount++;
            event("release-instance-complete",entry.getKey());
        }
    }
    public static Map<String,Object> teardownState() {
        requireOwner();
        Map<String,Object> sessions=new LinkedHashMap<>();
        sessionObjects.forEach((id,objects)->sessions.put(id,Map.of("window",Long.toUnsignedString(objects.window()),
            "instance",Long.toUnsignedString(objects.instance()),"released",sessionReleased(id))));
        return Map.of("mode",teardownMode,"sessionObjects",sessions,"pendingWindowHandles",deferredWindows.stream().map(Long::toUnsignedString).toList(),
            "pendingInstanceHandles",deferredInstances.keySet().stream().map(Long::toUnsignedString).toList(),
            "deferredWindowCount",deferredWindowCount,"deferredInstanceCount",deferredInstanceCount,
            "releasedWindowCount",releasedWindowCount,"releasedInstanceCount",releasedInstanceCount);
    }
    private static void event(String action,long handle) {
        System.err.println(EVENTS.toJson(Map.of("event","native-teardown","action",action,"handle",Long.toUnsignedString(handle),
            "mode",teardownMode,"pid",ProcessHandle.current().pid(),"ownerThreadId",Thread.currentThread().threadId(),
            "at",Instant.now().toString(),"pendingWindows",deferredWindows.size(),"pendingInstances",deferredInstances.size())));
        System.err.flush();
    }
}
