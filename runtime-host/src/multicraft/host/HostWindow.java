package multicraft.host;

import org.lwjgl.sdl.*;
import com.google.gson.Gson;
import java.nio.IntBuffer;
import java.util.*;

/** Owner-thread SDL display requests; native window geometry remains SDL-owned. */
public final class HostWindow {
    private static final class Profile {
        final int index;
        int target,actual;
        long window;
        boolean positionSupported;
        String positionError="";
        List<Long> enumerated=List.of();
        List<Map<String,Object>> displayDetails=List.of();
        int[] expectedOrigin;
        int nativeEnumerationIndex=-1;
        Profile(int index) { this.index=index; }
    }
    private static final Map<String,Profile> PROFILES=new LinkedHashMap<>();
    private static final Gson EVENTS=new Gson();
    private static Thread owner;
    private HostWindow() {}
    public static void configure(Thread thread,Map<String,Integer> indices) {
        if(owner!=null) throw new IllegalStateException("Window profiles already configured");
        owner=Objects.requireNonNull(thread);
        Set<Integer> unique=new HashSet<>();
        indices.forEach((id,index)->{
            if(index<0 || !unique.add(index)) throw new IllegalArgumentException("Each session requires a distinct nonnegative displayIndex");
            PROFILES.put(id,new Profile(index));
        });
    }
    private static Profile profile(String id) {
        if(Thread.currentThread()!=owner) throw new IllegalStateException("Native window operation outside owner thread");
        Profile profile=PROFILES.get(id);
        if(profile==null) throw new IllegalStateException("Unknown window profile "+id);
        return profile;
    }
    public static void expectOrigin(String id,int x,int y) {
        Profile profile=profile(id);
        if(profile.target!=0) throw new IllegalStateException("Display origin cannot change after selection");
        profile.expectedOrigin=new int[]{x,y};
    }
    public static int targetDisplay(String id) {
        Profile profile=profile(id);
        if(profile.target!=0) return profile.target;
        IntBuffer displays=SDLVideo.SDL_GetDisplays();
        if(displays==null) throw failure("Cannot enumerate SDL displays");
        try {
            List<Long> ids=new ArrayList<>();
            List<Map<String,Object>> details=new ArrayList<>();
            int matched=-1;
            try(SDL_Rect bounds=SDL_Rect.malloc()) {
                for(int i=displays.position();i<displays.limit();i++) {
                    int displayId=displays.get(i); ids.add(Integer.toUnsignedLong(displayId));
                    if(!SDLVideo.SDL_GetDisplayBounds(displayId,bounds)) throw failure("Cannot query native SDL display bounds");
                    details.add(Map.of("id",Integer.toUnsignedLong(displayId),"name",String.valueOf(SDLVideo.SDL_GetDisplayName(displayId)),
                        "x",bounds.x(),"y",bounds.y(),"width",bounds.w(),"height",bounds.h()));
                    if(profile.expectedOrigin!=null && bounds.x()==profile.expectedOrigin[0] && bounds.y()==profile.expectedOrigin[1]) {
                        if(matched!=-1) throw new IllegalStateException("Display origin is ambiguous for "+displayId);
                        matched=i;
                    }
                }
            }
            profile.enumerated=List.copyOf(ids);
            profile.displayDetails=List.copyOf(details);
            int selected=profile.expectedOrigin==null ? displays.position()+profile.index : matched;
            if(selected<displays.position() || selected>=displays.limit()) throw new IllegalStateException("Required display index/origin unavailable for "+id+"; SDL displays="+details);
            profile.nativeEnumerationIndex=selected-displays.position();
            int display=displays.get(selected);
            if(display==0 || (display&0xffff)!=display) throw new IllegalStateException("Display ID cannot be encoded by SDL centered-display position: "+Integer.toUnsignedString(display));
            profile.target=display;
            event("select-display",id,profile);
            return display;
        } finally { SDLStdinc.SDL_free(displays); }
    }
    /** Equivalent to SDL_CreateWindow's properties, with an explicit display position. */
    public static long create(String id,String title,int width,int height,long flags) {
        Profile profile=profile(id); int target=targetDisplay(id);
        int properties=SDLProperties.SDL_CreateProperties();
        if(properties==0) throw failure("Cannot create SDL window properties");
        try {
            int position=SDLVideo.SDL_WINDOWPOS_CENTERED_DISPLAY(target);
            if(!SDLProperties.SDL_SetStringProperty(properties,SDLVideo.SDL_PROP_WINDOW_CREATE_TITLE_STRING,title)
                || !SDLProperties.SDL_SetNumberProperty(properties,SDLVideo.SDL_PROP_WINDOW_CREATE_WIDTH_NUMBER,width)
                || !SDLProperties.SDL_SetNumberProperty(properties,SDLVideo.SDL_PROP_WINDOW_CREATE_HEIGHT_NUMBER,height)
                || !SDLProperties.SDL_SetNumberProperty(properties,SDLVideo.SDL_PROP_WINDOW_CREATE_FLAGS_NUMBER,flags)
                || !SDLProperties.SDL_SetNumberProperty(properties,SDLVideo.SDL_PROP_WINDOW_CREATE_X_NUMBER,position)
                || !SDLProperties.SDL_SetNumberProperty(properties,SDLVideo.SDL_PROP_WINDOW_CREATE_Y_NUMBER,position))
                throw failure("Cannot configure initial SDL window display");
            long window=SDLVideo.SDL_CreateWindowWithProperties(properties);
            if(window==0) throw failure("Cannot create SDL window on target display");
            profile.window=window; profile.actual=SDLVideo.SDL_GetDisplayForWindow(window);
            event("create-window",id,profile);
            return window;
        } finally { SDLProperties.SDL_DestroyProperties(properties); }
    }
    public static boolean setFullscreen(String id,long window,boolean fullscreen) {
        Profile profile=profile(id);
        if(profile.window!=window) throw new IllegalStateException("Fullscreen window does not belong to session "+id);
        if(fullscreen) {
            int position=SDLVideo.SDL_WINDOWPOS_CENTERED_DISPLAY(targetDisplay(id));
            // SDL records pending_displayID/last_position_pending before asking
            // Wayland to move a window; the next fullscreen request uses them.
            profile.positionSupported=SDLVideo.SDL_SetWindowPosition(window,position,position);
            profile.positionError=profile.positionSupported ? "" : String.valueOf(SDLError.SDL_GetError());
            if(!profile.positionSupported && !"wayland".equals(SDLVideo.SDL_GetCurrentVideoDriver()))
                throw failure("Cannot request fullscreen target display");
            event("request-fullscreen-display",id,profile);
        }
        return SDLVideo.SDL_SetWindowFullscreen(window,fullscreen);
    }
    public static void verify(String id,long window) {
        Profile profile=profile(id);
        if(profile.window!=window) throw new IllegalStateException("Registered window does not belong to session "+id);
        profile.actual=SDLVideo.SDL_GetDisplayForWindow(window);
        if(profile.actual!=targetDisplay(id)) {
            event("display-mismatch",id,profile);
            throw new IllegalStateException("Session "+id+" is on SDL display "+Integer.toUnsignedString(profile.actual)
                +"; required "+Integer.toUnsignedString(profile.target));
        }
    }
    public static Map<String,Object> state(String id,boolean live) {
        Profile profile=profile(id);
        if(live && profile.window!=0) profile.actual=SDLVideo.SDL_GetDisplayForWindow(profile.window);
        Map<String,Object> result=new LinkedHashMap<>();
        result.put("displayIndex",profile.index); result.put("targetDisplayId",Integer.toUnsignedLong(profile.target));
        result.put("actualDisplayId",Integer.toUnsignedLong(profile.actual)); result.put("enumeratedDisplayIds",profile.enumerated);
        result.put("nativeEnumerationIndex",profile.nativeEnumerationIndex); result.put("nativeDisplays",profile.displayDetails);
        if(profile.expectedOrigin!=null) result.put("expectedOrigin",List.of(profile.expectedOrigin[0],profile.expectedOrigin[1]));
        result.put("windowHandle",Long.toUnsignedString(profile.window));
        result.put("positionRequestSupported",profile.positionSupported); result.put("positionRequestError",profile.positionError);
        return result;
    }
    private static IllegalStateException failure(String message) { return new IllegalStateException(message+": "+SDLError.SDL_GetError()); }
    private static void event(String action,String id,Profile profile) {
        System.err.println(EVENTS.toJson(Map.of("event","native-display","action",action,"session",id,
            "displayIndex",profile.index,"targetDisplayId",Integer.toUnsignedLong(profile.target),
            "actualDisplayId",Integer.toUnsignedLong(profile.actual),"windowHandle",Long.toUnsignedString(profile.window),
            "positionRequestSupported",profile.positionSupported,"positionRequestError",profile.positionError)));
        System.err.flush();
    }
}
