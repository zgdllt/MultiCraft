package multicraft.host;

import com.google.gson.*;
import com.sun.net.httpserver.*;
import org.lwjgl.sdl.*;

import java.io.*;
import java.lang.reflect.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.security.MessageDigest;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.locks.LockSupport;

/** One owner thread serially drives a configured pool of independent native client realms. */
public final class Host {
    private static final Gson JSON = new GsonBuilder().disableHtmlEscaping().create();
    private static final Map<String, Session> SESSIONS = new LinkedHashMap<>();
    private static final Map<Long, Session> WINDOWS = new HashMap<>();
    private static final BlockingQueue<Runnable> COMMANDS = new ArrayBlockingQueue<>(512);
    private static final CountDownLatch CLEANUP_COMPLETED = new CountDownLatch(1);
    private static Thread owner;
    private static Session active;
    private static volatile boolean shutdown;
    private static boolean polling;
    private static HttpServer server;
    private static String token;
    private static Path stateFile;
    private static long cycles;
    private static String audioMode;
    private static String requiredGraphicsBackend,requiredGraphicsVendor;
    private static String sdlQuitPolicy="native",sdlQuitStatus="not-requested";
    private static boolean nativeSdlQuitCalled,nativeSdlQuitCompleted,allNativeResourcesReleased;
    private static final String RUNTIME_OWNER = System.getProperty("multicraft.owner", UUID.randomUUID().toString());

    private static final class Yield extends Error {
        Yield() { super(null, null, false, false); }
    }
    private static final class SessionExit extends Error {
        final int code;
        SessionExit(int code) { super("Realm requested exit " + code, null, false, false); this.code = code; }
    }
    private static final class Session {
        final String id;
        final RealmLoader loader;
        final String[] arguments;
        final Set<Integer> keys = new HashSet<>();
        final Set<Integer> buttons = new HashSet<>();
        final String address;
        final int displayIndex;
        int[] displayOrigin;
        Object minecraft, eventHandler, window;
        Method tick;
        long handle, frames, inputEvents;
        long connectionEpoch=1,lastInputSeq;
        Object lastConnection;
        Object networkConnection;
        Map<String,Object> graphics=Map.of();
        boolean connectionObserved;
        int windowId, modifiers;
        int requestedFrameLimit=60;
        long frameConfigSequence;
        long nextFrameNanos;
        String failure;
        String clipboard = "";
        boolean closed,nativeCleanupComplete,audioCleanupComplete;
        Session(String id, RealmLoader loader, String[] arguments, String address,int displayIndex) {
            this.id = id; this.loader = loader; this.arguments = arguments; this.address = address; this.displayIndex=displayIndex;
        }
    }
    private Host() {}

    public static void main(String[] args) throws Exception {
        if (args.length != 1) throw new IllegalArgumentException("Usage: multicraft.host.Host CONFIG.json");
        JsonObject config = JsonParser.parseString(Files.readString(Path.of(args[0]))).getAsJsonObject();
        audioMode=required(config,"audioMode");
        requiredGraphicsBackend=config.has("requiredGraphicsBackend") ? required(config,"requiredGraphicsBackend") : "Vulkan";
        requiredGraphicsVendor=config.has("requiredGraphicsVendor") ? required(config,"requiredGraphicsVendor") : "";
        sdlQuitPolicy=config.has("sdlQuitPolicy") ? required(config,"sdlQuitPolicy") : "native";
        if(!Set.of("native","process-exit").contains(sdlQuitPolicy)) throw new IllegalArgumentException("Unknown sdlQuitPolicy");
        if (!Set.of("disabled-probe","loopback-pcm").contains(audioMode)) throw new IllegalArgumentException("Unknown explicit audioMode");
        Path client = Path.of(required(config, "clientJar")).toRealPath();
        if (!sha256(client).equals(FixedVersionTransform.CLIENT_SHA256)) throw new IllegalArgumentException("Pinned client hash mismatch");
        token = Files.readString(Path.of(required(config, "tokenFile"))).strip();
        if (token.length() < 32) throw new IllegalArgumentException("Control token must have at least 32 characters");
        stateFile = Path.of(required(config, "stateFile"));
        List<URL> urls = new ArrayList<>();
        // The client is first so accidental duplicate game classes in libraries cannot win.
        urls.add(client.toUri().toURL());
        for (JsonElement item : config.getAsJsonArray("libraries")) urls.add(Path.of(item.getAsString()).toRealPath().toUri().toURL());
        JsonArray sessions = config.getAsJsonArray("sessions");
        if (sessions.isEmpty() || sessions.size()>16) throw new IllegalArgumentException("Native pool requires 1..16 sessions");
        Set<Path> directories = new HashSet<>();
        Set<String> usernames=new HashSet<>(),uuids=new HashSet<>();
        for (JsonElement item : sessions) {
            JsonObject s = item.getAsJsonObject();
            String id = required(s, "id");
            if (!id.matches("[A-Za-z0-9_-]{1,32}") || SESSIONS.containsKey(id)) throw new IllegalArgumentException("Invalid or duplicate session id");
            String[] launch = new String[s.getAsJsonArray("args").size()];
            for (int i = 0; i < launch.length; i++) launch[i] = s.getAsJsonArray("args").get(i).getAsString();
            Path game = gameDirectory(launch).toRealPath();
            if (!directories.add(game)) throw new IllegalArgumentException("Sessions must have independent writable game directories");
            String username=argument(launch,"--username"),uuid=UUID.fromString(argument(launch,"--uuid")).toString();
            if(!username.matches("[A-Za-z0-9_]{1,16}") || !usernames.add(username.toLowerCase(Locale.ROOT)) || !uuids.add(uuid))
                throw new IllegalArgumentException("Native sessions require unique valid Minecraft names and UUIDs");
            String address = required(s, "serverAddress");
            URI target=URI.create("minecraft://"+address);
            if(address.length()>300 || target.getHost()==null || target.getPort()<1 || target.getPort()>65535
                || target.getRawUserInfo()!=null || target.getRawQuery()!=null || target.getRawFragment()!=null
                || (target.getRawPath()!=null && !target.getRawPath().isEmpty()))
                throw new IllegalArgumentException("An explicit hostname:port or [IPv6]:port server address is required");
            boolean disabled=audioMode.equals("disabled-probe");
            if(!disabled) HostAudio.register(id);
            int displayIndex=s.has("displayIndex") ? s.get("displayIndex").getAsInt() : SESSIONS.size();
            Session session=new Session(id, new RealmLoader(id, urls.toArray(URL[]::new), disabled), launch, address,displayIndex);
            if(s.has("displayOrigin")) {
                JsonArray origin=s.getAsJsonArray("displayOrigin");
                if(origin.size()!=2) throw new IllegalArgumentException("displayOrigin must have x,y");
                JsonObject coordinates=new JsonObject(); coordinates.add("x",origin.get(0)); coordinates.add("y",origin.get(1));
                session.displayOrigin=new int[]{integer(coordinates,"x"),integer(coordinates,"y")};
            }
            session.requestedFrameLimit=s.has("initialFrameLimit") ? integer(s,"initialFrameLimit") : 60;
            if(session.requestedFrameLimit<10 || session.requestedFrameLimit>1000) throw new IllegalArgumentException("initialFrameLimit requires 10..1000");
            SESSIONS.put(id,session);
        }
        owner = Thread.currentThread();
        owner.setName("MultiCraft owner render thread");
        Map<String,Integer> displayIndices=new LinkedHashMap<>(); SESSIONS.forEach((id,s)->displayIndices.put(id,s.displayIndex));
        HostWindow.configure(owner,displayIndices);
        SESSIONS.forEach((id,s)->{ if(s.displayOrigin!=null) HostWindow.expectOrigin(id,s.displayOrigin[0],s.displayOrigin[1]); });
        HostGraphics.configureTeardown(config.has("teardownMode") ? required(config,"teardownMode") : "standard",owner);
        startControl(config.get("controlPort").getAsInt());
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            shutdown = true;
            // SIGTERM starts hooks concurrently with main. Native cleanup stays on
            // its owner, while this hook keeps the JVM alive for a bounded drain.
            try { CLEANUP_COMPLETED.await(30, TimeUnit.SECONDS); }
            catch (InterruptedException e) { Thread.currentThread().interrupt(); }
        }, "MultiCraft shutdown request"));
        try {
            writeState();
            for (Session s : SESSIONS.values()) {
                if (shutdown) break;
                activate(s);
                try {
                    s.loader.loadClass("net.minecraft.client.main.Main").getMethod("main", String[].class).invoke(null, (Object)s.arguments);
                    throw new IllegalStateException("Original Main returned before native registration");
                } catch (InvocationTargetException e) {
                    if (!(e.getCause() instanceof Yield)) throw new RuntimeException(rootCause(e));
                }
                if (s.minecraft == null || !Boolean.TRUE.equals(call(s.minecraft, "isRunning")))
                    throw new IllegalStateException("Realm did not register a running original client");
                writeState();
            }
            long maxCycles = config.has("maxCycles") ? config.get("maxCycles").getAsLong() : 0;
            long previousSave = 0;
            while (!shutdown && SESSIONS.values().stream().anyMatch(s -> !s.closed)) {
                drainCommands();
                pollGlobal();
                long earliest=Long.MAX_VALUE;
                for (Session s : SESSIONS.values()) {
                    if (s.closed) continue;
                    activate(s);
                    try {
                        if (!Boolean.TRUE.equals(call(s.minecraft, "isRunning"))) { close(s); continue; }
                        long frameStart=System.nanoTime();
                        if(frameStart<s.nextFrameNanos) { earliest=Math.min(earliest,s.nextFrameNanos); continue; }
                        s.tick.invoke(s.minecraft, true);
                        HostWindow.verify(s.id,s.handle);
                        s.frames++;
                        int nativeLimit=((Number)call(field(s.minecraft,"framerateLimitTracker"),"getFramerateLimit")).intValue();
                        int limit=nativeLimit>=260 ? s.requestedFrameLimit : Math.min(s.requestedFrameLimit,Math.max(1,nativeLimit));
                        s.nextFrameNanos=FrameSchedule.nextDeadline(s.nextFrameNanos,frameStart,limit);
                        earliest=Math.min(earliest,s.nextFrameNanos);
                    } catch (Throwable e) {
                        s.failure = describe(rootCause(e));
                        System.err.println("MultiCraft realm " + s.id + " stopped: " + s.failure);
                        rootCause(e).printStackTrace(System.err);
                        close(s);
                    }
                }
                cycles++;
                if (maxCycles > 0 && cycles >= maxCycles) shutdown = true;
                if (System.nanoTime() - previousSave > 500_000_000L) { writeState(); previousSave = System.nanoTime(); }
                // Per-slot deadlines replace the original blocking display limiter.
                // Never sleep for one slow stream while another slot is due.
                long wait=earliest-System.nanoTime();
                if(wait>0 && earliest!=Long.MAX_VALUE) LockSupport.parkNanos(Math.min(wait,2_000_000L));
            }
        } finally {
            try {
                for (Session s : SESSIONS.values()) if (s.minecraft != null && !s.closed) close(s);
                try { HostGraphics.releaseDeferred(); }
                finally { try { writeState(); } catch (Exception e) { System.err.println("Final state write failed: " + e); } }
            } finally {
                try {
                    if (server != null) server.stop(0);
                } finally {
                    try { finishSdl(); }
                    finally {
                        try {
                            for (Session s : SESSIONS.values()) try { s.loader.close(); }
                            catch (IOException e) { System.err.println("Realm loader close failed: " + e); }
                        } finally { CLEANUP_COMPLETED.countDown(); }
                    }
                }
            }
        }
    }

    /** Replaces Main's original final run call; the sentinel never enters vanilla cleanup. */
    public static void registerAndYield(Object minecraft) {
        requireOwner();
        if (active == null || active.minecraft != null) throw new IllegalStateException("Unexpected native registration");
        try {
            active.minecraft = minecraft;
            active.window = call(minecraft, "getWindow");
            active.handle = ((Number)call(active.window, "handle")).longValue();
            active.windowId = SDLVideo.SDL_GetWindowID(active.handle);
            active.eventHandler = field(minecraft, "sdlEventHandler");
            active.tick = minecraft.getClass().getDeclaredMethod("runTick", boolean.class);
            active.tick.setAccessible(true);
            Object gameThread = field(minecraft, "gameThread");
            if (gameThread != owner) throw new IllegalStateException("Client constructed on another game thread");
            Object device=active.loader.loadClass("com.mojang.blaze3d.systems.RenderSystem").getMethod("getDevice").invoke(null);
            Object info=active.loader.loadClass("com.mojang.renderpearl.api.device.GpuDevice").getMethod("getDeviceInfo").invoke(device);
            Map<String,Object> graphics=new LinkedHashMap<>();
            for(String key:List.of("name","vendorName","driverInfo","backendName","type")) graphics.put(key,String.valueOf(call(info,key)));
            graphics.put("deviceIdentity",System.identityHashCode(device)); active.graphics=Map.copyOf(graphics);
            if(!requiredGraphicsBackend.isBlank() && !requiredGraphicsBackend.equalsIgnoreCase((String)graphics.get("backendName")))
                throw new IllegalStateException("Session "+active.id+" selected unexpected graphics backend: "+graphics+"; required "+requiredGraphicsBackend);
            if(!requiredGraphicsVendor.isBlank() && !requiredGraphicsVendor.equalsIgnoreCase((String)graphics.get("vendorName")))
                throw new IllegalStateException("Session "+active.id+" selected unexpected graphics vendor: "+graphics+"; required "+requiredGraphicsVendor);
            Session previous = WINDOWS.putIfAbsent(active.handle, active);
            if (previous != null && previous != active) throw new IllegalStateException("Duplicate native window");
            HostWindow.verify(active.id,active.handle);
            Map<String,Object> nativeObjects=nativeGraphicsSnapshot(active);
            HostGraphics.registerSession(active.id,active.handle,nativeObjects.containsKey("nativeInstance")
                ? Long.parseUnsignedLong((String)nativeObjects.get("nativeInstance")) : 0);
            call(field(active.minecraft,"framerateLimitTracker"),"setFramerateLimit",new Class<?>[]{int.class},active.requestedFrameLimit);
            call(active.window, "setTitle", new Class<?>[]{String.class}, "MultiCraft M1 " + active.id);
        } catch (ReflectiveOperationException e) { throw new IllegalStateException(e); }
        throw new Yield();
    }

    public static void requestExit(int code) { throw new SessionExit(code); }
    public static void hostOwnsFrameWait(int ignoredNativeLimit) { requireOwner(); /* Pool due scheduler owns this wait. */ }
    public static void deferSdlQuit() { /* Only Host.main's final cleanup owns process-global SDL_Quit. */ }
    public static void deferBackendUnload() { /* Parent-owned JNI libraries live until the host process exits. */ }
    public static short modifierState() { requireOwner(); return (short)(active == null ? 0 : active.modifiers); }
    public static boolean isKeyDown(int scancode) { requireOwner(); return active != null && active.keys.contains(scancode); }
    public static String getClipboard() { requireOwner(); return active == null ? "" : active.clipboard; }
    public static void setClipboard(String value) { requireOwner(); if (active == null) throw new IllegalStateException("No active realm"); active.clipboard = Objects.requireNonNull(value); }
    public static String titleFor(String ignored) { requireOwner(); return active==null ? "MultiCraft" : "MultiCraft "+active.id; }
    public static int initialDisplay() { requireOwner(); return HostWindow.targetDisplay(active.id); }
    public static long createNativeWindow(CharSequence title,int width,int height,long flags) {
        requireOwner(); return HostWindow.create(active.id,titleFor(String.valueOf(title)),width,height,flags);
    }
    public static boolean setNativeFullscreen(long window,boolean fullscreen) {
        requireOwner(); return HostWindow.setFullscreen(active.id,window,fullscreen);
    }
    public static void pollEventsFor(Object handler) {
        requireOwner();
        try {
            if (active != null && active.eventHandler == null) {
                // Initial native loading frames can poll before Main reaches registration.
                // Preserve their resize/fullscreen notifications instead of losing the window.
                active.eventHandler = handler;
                active.window = field(handler,"window");
                active.handle = ((Number)call(active.window,"handle")).longValue();
                active.windowId = SDLVideo.SDL_GetWindowID(active.handle);
                Session previous = WINDOWS.putIfAbsent(active.handle,active);
                if (previous != null && previous != active) throw new IllegalStateException("Duplicate native window");
            }
            pollGlobal();
        } catch (ReflectiveOperationException e) { throw new IllegalStateException(e); }
    }
    public static void flushEventsFor(Object handler) {
        requireOwner();
        // SDL_FlushEvents would discard another player's pending events. Owner dispatch is immediate.
    }
    private static void pollGlobal() throws ReflectiveOperationException {
        requireOwner();
        if (polling) return;
        polling = true;
        Session previous = active;
        try (SDL_Event event = SDL_Event.malloc()) {
            int budget = 4096;
            while (budget-- > 0 && SDLEvents.SDL_PollEvent(event)) {
                long handle = SDLEvents.SDL_GetWindowFromEvent(event);
                Session target = WINDOWS.get(handle);
                if (target != null && !target.closed) {
                    activate(target); dispatch(target, event);
                } else if (handle == 0 && event.type() != 256) {
                    // Only native process/display notifications broadcast; input always has a window.
                    int type = event.type();
                    if (type == 772 || (type >= 337 && type <= 344))
                        for (Session s : SESSIONS.values()) if (!s.closed && s.window != null) { activate(s); dispatch(s, event); }
                }
            }
        } finally { if (previous != null) activate(previous); polling = false; }
    }
    private static void dispatch(Session s, SDL_Event event) throws ReflectiveOperationException {
        if (s.eventHandler == null) return;
        String method = switch(event.type()) {
            case 768, 769 -> "handleKeyEvent";
            case 770 -> "handleTextEditingEvent";
            case 771 -> "handleTextInputEvent";
            case 772 -> "handleKeymapChangedEvent";
            case 1024 -> "handleMouseMotionEvent";
            case 1025, 1026 -> "handleMouseButtonEvent";
            case 1027 -> "handleMouseWheelEvent";
            case 4096 -> "handleDropFileEvent";
            case 4098 -> "handleDropBeginEvent";
            case 4099 -> "handleDropCompleteEvent";
            default -> null;
        };
        if (event.type() == 768 || event.type() == 769) {
            if (event.type() == 768) s.keys.add(event.key().scancode()); else s.keys.remove(event.key().scancode());
            s.modifiers = event.key().mod();
        }
        if (event.type() == 1025) s.buttons.add((int)event.button().button());
        if (event.type() == 1026) s.buttons.remove((int)event.button().button());
        if (method == null) call(s.window, "handleEvent", new Class<?>[]{SDL_Event.class}, event);
        else if (event.type() == 772 || event.type() == 4098) privateCall(s.eventHandler, method, new Class<?>[0]);
        else privateCall(s.eventHandler, method, new Class<?>[]{SDL_Event.class}, event);
    }

    private static void startControl(int port) throws IOException {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", port), 8);
        server.createContext("/", Host::http);
        server.setExecutor(Executors.newFixedThreadPool(2, r -> { Thread t = new Thread(r, "MultiCraft control HTTP"); t.setDaemon(true); return t; }));
        server.start();
    }
    private static void http(HttpExchange exchange) throws IOException {
        try {
            String auth = exchange.getRequestHeaders().getFirst("Authorization");
            if (auth == null || !MessageDigest.isEqual(auth.getBytes(StandardCharsets.UTF_8), ("Bearer " + token).getBytes(StandardCharsets.UTF_8))) {
                respond(exchange, 401, Map.of("error", "unauthorized")); return;
            }
            String path = exchange.getRequestURI().getPath();
            String[] audioParts=path.split("/");
            if(exchange.getRequestMethod().equals("GET") && audioParts.length==4 && audioParts[1].equals("session") &&
                    (audioParts[3].equals("audio") || audioParts[3].equals("audio-state"))) {
                if(!SESSIONS.containsKey(audioParts[2])) { respond(exchange,404,Map.of("error","Unknown session")); return; }
                if(!audioMode.equals("loopback-pcm")) { respond(exchange,409,Map.of("error","Audio is disabled for this probe")); return; }
                if(audioParts[3].equals("audio-state")) { respond(exchange,200,HostAudio.state(audioParts[2])); return; }
                Map<String,String> query=new HashMap<>();
                String raw=exchange.getRequestURI().getRawQuery();
                if(raw!=null) for(String item:raw.split("&")) { String[] pair=item.split("=",2); if(pair.length==2) query.put(pair[0],pair[1]); }
                HostAudio.Chunk chunk=HostAudio.read(audioParts[2],Long.parseLong(query.getOrDefault("epoch","-1")),
                    Long.parseLong(query.getOrDefault("sinceFrame","0")),Integer.parseInt(query.getOrDefault("maxFrames","4800")));
                var headers=exchange.getResponseHeaders();
                headers.set("Content-Type","application/octet-stream"); headers.set("Cache-Control","no-store");
                headers.set("X-MultiCraft-Audio-Epoch",Long.toString(chunk.epoch()));
                headers.set("X-MultiCraft-Audio-Start-Frame",Long.toString(chunk.startFrame()));
                headers.set("X-MultiCraft-Audio-End-Frame",Long.toString(chunk.endFrame()));
                headers.set("X-MultiCraft-Audio-Dropped-Frames",Long.toString(chunk.droppedFrames()));
                headers.set("X-MultiCraft-Audio-Sample-Rate","48000");
                exchange.sendResponseHeaders(200,chunk.data().length); exchange.getResponseBody().write(chunk.data()); return;
            }
            JsonObject body = new JsonObject();
            if (exchange.getRequestMethod().equals("POST")) {
                byte[] bytes = exchange.getRequestBody().readNBytes(65537);
                if (bytes.length > 65536) { respond(exchange, 413, Map.of("error", "body too large")); return; }
                if (bytes.length != 0) body = JsonParser.parseString(new String(bytes, StandardCharsets.UTF_8)).getAsJsonObject();
            }
            JsonObject command = body;
            CompletableFuture<Object> result = new CompletableFuture<>();
            Runnable work = () -> {
                try {
                    if (path.equals("/state") && exchange.getRequestMethod().equals("GET")) result.complete(snapshot());
                    else if (path.equals("/shutdown") && exchange.getRequestMethod().equals("POST")) { shutdown = true; result.complete(Map.of("ok", true)); }
                    else {
                        String[] parts = path.split("/");
                        if (parts.length != 4 || !parts[1].equals("session") || !exchange.getRequestMethod().equals("POST")) throw new IllegalArgumentException("Unknown route");
                        Session s = SESSIONS.get(parts[2]);
                        if (s == null || s.minecraft == null || s.closed) throw new IllegalArgumentException("Session is not active");
                        activate(s);
                        if (command.has("expectedRuntimeOwner") &&
                                !RUNTIME_OWNER.equals(required(command,"expectedRuntimeOwner")))
                            throw new IllegalArgumentException("Stale runtime owner");
                        if(!parts[3].equals("input") && !parts[3].equals("command")) throw new IllegalArgumentException("Unknown route");
                        refreshConnectionEpoch(s,call(s.minecraft,"getConnection"));
                        if(command.has("expectedEpoch") && safeSequence(command,"expectedEpoch")!=s.connectionEpoch)
                            throw new IllegalArgumentException("Stale connection epoch; current="+s.connectionEpoch);
                        if (parts[3].equals("input")) {
                            if(command.has("inputSeq")) {
                                if(!command.has("expectedEpoch")) throw new IllegalArgumentException("inputSeq requires expectedEpoch");
                                long seq=safeSequence(command,"inputSeq");
                                if(seq<=s.lastInputSeq) throw new IllegalArgumentException("Stale input sequence");
                                s.lastInputSeq=seq;
                            }
                            input(s, command);
                        }
                        else command(s, command);
                        result.complete(Map.of("ok", true, "session", s.id, "inputEvents", s.inputEvents,
                            "connectionEpoch",s.connectionEpoch,"lastInputSeq",s.lastInputSeq,
                            "nativeFrameLimit",s.requestedFrameLimit,"frameConfigSequence",s.frameConfigSequence));
                    }
                } catch (Throwable e) { result.completeExceptionally(rootCause(e)); }
            };
            if (!COMMANDS.offer(work)) { respond(exchange, 429, Map.of("error", "owner queue full")); return; }
            try { respond(exchange, 200, result.get(15, TimeUnit.SECONDS)); }
            catch (TimeoutException e) { respond(exchange, 503, Map.of("error", "owner thread busy; command may still execute")); }
            catch (ExecutionException e) { respond(exchange, 400, Map.of("error", describe(e.getCause()))); }
            catch (InterruptedException e) { Thread.currentThread().interrupt(); respond(exchange, 503, Map.of("error", "interrupted")); }
        } catch (RuntimeException e) { respond(exchange, 400, Map.of("error", describe(e))); }
        finally { exchange.close(); }
    }
    private static void input(Session s, JsonObject event) throws ReflectiveOperationException {
        if (event.has("events")) {
            if (event.getAsJsonArray("events").size() > 128) throw new IllegalArgumentException("Too many input events");
            for (JsonElement child : event.getAsJsonArray("events")) {
                JsonObject value = child.getAsJsonObject();
                if (value.has("events")) throw new IllegalArgumentException("Input batches must be flat");
                validateInput(value);
            }
            for (JsonElement child : event.getAsJsonArray("events")) input(s, child.getAsJsonObject());
            return;
        }
        validateInput(event);
        Object mouse = field(s.minecraft, "mouseHandler"), keyboard = field(s.minecraft, "keyboardHandler");
        String type = required(event, "type");
        switch (type) {
            case "key" -> {
                int scan = integer(event, "scancode"), action = integer(event, "action");
                if (scan < 0 || scan > 512 || action < -1 || action > 1) throw new IllegalArgumentException("Invalid scancode/action");
                if (action == 0) s.keys.remove(scan); else s.keys.add(scan);
                s.modifiers = event.has("modifiers") ? integer(event, "modifiers") : 0;
                Class<?> key = s.loader.loadClass("net.minecraft.client.input.KeyEvent");
                Object value = key.getConstructor(int.class, int.class, int.class).newInstance(scan, integer(event, "keycode"), s.modifiers);
                call(keyboard, "keyPress", new Class<?>[]{long.class, int.class, key}, s.handle, action, value);
            }
            case "text" -> call(keyboard, "textInput", new Class<?>[]{long.class, String.class}, s.handle, required(event, "text"));
            case "move" -> call(mouse, "onMove", new Class<?>[]{long.class,double.class,double.class,double.class,double.class},
                s.handle, number(event,"x"), number(event,"y"), optionalNumber(event,"dx"), optionalNumber(event,"dy"));
            case "button" -> {
                int buttonId = integer(event,"button"), action = integer(event,"action");
                if (buttonId < 1 || buttonId > 8 || action < 0 || action > 1) throw new IllegalArgumentException("Invalid mouse button/action");
                if (action == 1) s.buttons.add(buttonId); else s.buttons.remove(buttonId);
                Class<?> button = s.loader.loadClass("net.minecraft.client.input.MouseButtonInfo");
                Object value = button.getConstructor(int.class,int.class).newInstance(buttonId,s.modifiers);
                call(mouse,"onButton",new Class<?>[]{long.class,button,int.class},s.handle,value,action);
            }
            case "scroll" -> call(mouse,"onScroll",new Class<?>[]{long.class,double.class,double.class},s.handle,number(event,"x"),number(event,"y"));
            case "release" -> {
                Class<?> key = s.loader.loadClass("net.minecraft.client.input.KeyEvent");
                for (int scan : new HashSet<>(s.keys)) {
                    Object value = key.getConstructor(int.class,int.class,int.class).newInstance(scan,0,0);
                    call(keyboard,"keyPress",new Class<?>[]{long.class,int.class,key},s.handle,0,value);
                }
                s.keys.clear(); s.modifiers = 0;
                Class<?> button = s.loader.loadClass("net.minecraft.client.input.MouseButtonInfo");
                for (int buttonId : new HashSet<>(s.buttons)) {
                    Object value = button.getConstructor(int.class,int.class).newInstance(buttonId,0);
                    call(mouse,"onButton",new Class<?>[]{long.class,button,int.class},s.handle,value,0);
                }
                s.buttons.clear();
                call(mouse,"releaseMouse");
            }
            case "grab" -> call(mouse,"grabMouse");
            default -> throw new IllegalArgumentException("Unknown input type");
        }
        s.inputEvents++;
    }
    private static void validateInput(JsonObject event) {
        switch(required(event,"type")) {
            case "key" -> {
                int scan=integer(event,"scancode"), action=integer(event,"action");
                integer(event,"keycode");
                if(scan<0 || scan>512 || action< -1 || action>1) throw new IllegalArgumentException("Invalid scancode/action");
                if(event.has("modifiers")) { int mods=integer(event,"modifiers"); if(mods<0 || mods>65535) throw new IllegalArgumentException("Invalid modifiers"); }
            }
            case "button" -> {
                int id=integer(event,"button"), action=integer(event,"action");
                if(id<1 || id>8 || action<0 || action>1) throw new IllegalArgumentException("Invalid mouse button/action");
            }
            case "move" -> { number(event,"x"); number(event,"y"); optionalNumber(event,"dx"); optionalNumber(event,"dy"); }
            case "scroll" -> { number(event,"x"); number(event,"y"); }
            case "text" -> { if(required(event,"text").length()>16384) throw new IllegalArgumentException("Text input too large"); }
            case "grab", "release" -> {}
            default -> throw new IllegalArgumentException("Unknown input type");
        }
    }
    private static void command(Session s, JsonObject command) throws ReflectiveOperationException {
        String kind=required(command,"type");
        if(kind.equals("connect") || kind.equals("disconnect")) {
            JsonObject release=new JsonObject(); release.addProperty("type","release"); input(s,release);
            s.connectionEpoch++; s.lastInputSeq=0;
        }
        switch(kind) {
            case "close" -> close(s);
            case "setFrameLimit" -> {
                int fps=integer(command,"fps");
                if(fps<10 || fps>1000) throw new IllegalArgumentException("Native frame target requires 10..1000");
                long sequence=command.has("configSequence") ? safeSequence(command,"configSequence") : s.frameConfigSequence+1;
                if(sequence<=s.frameConfigSequence) throw new IllegalArgumentException("Stale frame configuration sequence");
                call(field(s.minecraft,"framerateLimitTracker"),"setFramerateLimit",new Class<?>[]{int.class},fps);
                s.requestedFrameLimit=fps; s.frameConfigSequence=sequence; s.nextFrameNanos=0;
            }
            case "disconnect" -> {
                try { disconnectNetwork(s,"Quitting MultiCraft session"); }
                finally { call(s.minecraft,"disconnectWithSavingScreen"); }
            }
            case "reloadResources" -> call(s.minecraft,"reloadResourcePacks");
            case "respawn" -> { Object player = field(s.minecraft,"player"); if (player == null) throw new IllegalStateException("No player"); call(player,"respawn"); }
            case "connect" -> {
                disconnectNetwork(s,"Reconnecting MultiCraft session");
                Class<?> screen = s.loader.loadClass("net.minecraft.client.gui.screens.Screen");
                Class<?> data = s.loader.loadClass("net.minecraft.client.multiplayer.ServerData");
                Class<?> dataType = s.loader.loadClass("net.minecraft.client.multiplayer.ServerData$Type");
                Object serverKind = dataType.getEnumConstants()[0];
                for (Object value : dataType.getEnumConstants()) if (value.toString().equals("OTHER")) serverKind = value;
                Object serverData = data.getConstructor(String.class,String.class,dataType).newInstance("MultiCraft M1",s.address,serverKind);
                Class<?> address = s.loader.loadClass("net.minecraft.client.multiplayer.resolver.ServerAddress");
                Object value = address.getMethod("parseString",String.class).invoke(null,s.address);
                Class<?> transfer = s.loader.loadClass("net.minecraft.client.multiplayer.TransferState");
                s.loader.loadClass("net.minecraft.client.gui.screens.ConnectScreen").getMethod("startConnecting",screen,s.minecraft.getClass(),address,data,boolean.class,transfer)
                    .invoke(null,null,s.minecraft,value,serverData,true,null);
            }
            default -> throw new IllegalArgumentException("Unknown native command");
        }
    }
    private static Map<String,Object> snapshot() throws ReflectiveOperationException {
        requireOwner();
        Map<String,Object> out = new LinkedHashMap<>();
        out.put("schema",1); out.put("pid",ProcessHandle.current().pid()); out.put("ownerThread",owner.getName());
        out.put("ownerThreadId",owner.threadId()); out.put("cycles",cycles); out.put("audioMode",audioMode);
        out.put("runtimeOwner",RUNTIME_OWNER);
        out.put("graphicsPolicy",Map.of("requiredBackend",requiredGraphicsBackend,"requiredVendor",requiredGraphicsVendor));
        out.put("graphicsTeardown",HostGraphics.teardownState());
        out.put("sdlShutdown",Map.of("policy",sdlQuitPolicy,"status",sdlQuitStatus,
            "nativeQuitCalled",nativeSdlQuitCalled,"nativeQuitCompleted",nativeSdlQuitCompleted,
            "allNativeResourcesReleased",allNativeResourcesReleased,"globalResourceCleanup",
            sdlQuitPolicy.equals("process-exit") ? "operating-system-on-process-exit" : "native-SDL-Quit"));
        out.put("m1Complete",false); out.put("graphicsSharing",false); out.put("clientSha256",FixedVersionTransform.CLIENT_SHA256);
        out.put("capabilities",Map.of("sessionCount",SESSIONS.size(),"maximumSessions",16,"sameJvm",true,"dynamicSlots",false,"graphicsResourceSharing",false));
        List<Object> states = new ArrayList<>();
        for (Session s : SESSIONS.values()) {
            Map<String,Object> state = new LinkedHashMap<>();
            state.put("id",s.id); state.put("realm",s.loader.getName()); state.put("frames",s.frames); state.put("closed",s.closed);
            state.put("nativeCleanupComplete",s.nativeCleanupComplete); state.put("audioCleanupComplete",s.audioCleanupComplete);
            state.put("requestedFrameLimit",s.requestedFrameLimit);
            state.put("frameConfigSequence",s.frameConfigSequence);
            state.put("display",HostWindow.state(s.id,s.minecraft!=null && !s.closed));
            state.put("failure",s.failure); state.put("windowHandle",Long.toUnsignedString(s.handle)); state.put("windowId",s.windowId);
            state.put("inputEvents",s.inputEvents); state.put("keys",new ArrayList<>(s.keys));
            state.put("graphics",s.closed || s.minecraft==null ? s.graphics : nativeGraphicsSnapshot(s));
            state.put("networkConnected",s.networkConnection!=null && Boolean.TRUE.equals(call(s.networkConnection,"isConnected")));
            if(audioMode.equals("loopback-pcm")) state.put("audio",HostAudio.state(s.id));
            if (s.minecraft != null && !s.closed) {
                activate(s);
                state.put("minecraftIdentity",System.identityHashCode(s.minecraft));
                state.put("running",call(s.minecraft,"isRunning"));
                state.put("nativeFps",call(s.minecraft,"getFps")); state.put("frameTimeNs",call(s.minecraft,"getFrameTimeNs"));
                state.put("framerate",framerateSnapshot(s));
                Thread thread = (Thread)field(s.minecraft,"gameThread"); state.put("gameThreadId",thread.threadId());
                Object player = field(s.minecraft,"player"), level = field(s.minecraft,"level"), gui = field(s.minecraft,"gui");
                Object connection = call(s.minecraft,"getConnection"), screen = call(gui,"screen");
                refreshConnectionEpoch(s,connection);
                state.put("networkConnected",s.networkConnection!=null && Boolean.TRUE.equals(call(s.networkConnection,"isConnected")));
                state.put("playerIdentity",player == null ? null : System.identityHashCode(player));
                state.put("levelIdentity",level == null ? null : System.identityHashCode(level));
                state.put("guiIdentity",System.identityHashCode(gui)); state.put("screen",screen == null ? null : screen.getClass().getName());
                state.put("connectionIdentity",connection == null ? null : System.identityHashCode(connection));
                state.put("connectionEpoch",s.connectionEpoch); state.put("lastInputSeq",s.lastInputSeq);
                state.put("width",call(s.window,"getWidth")); state.put("height",call(s.window,"getHeight"));
                state.put("mouseGrabbed",call(field(s.minecraft,"mouseHandler"),"isMouseGrabbed"));
                if (player != null) {
                    state.put("uuid",String.valueOf(call(player,"getUUID"))); state.put("health",call(player,"getHealth"));
                    state.put("x",call(player,"getX")); state.put("y",call(player,"getY")); state.put("z",call(player,"getZ"));
                    state.put("yaw",call(player,"getYRot")); state.put("pitch",call(player,"getXRot"));
                    state.put("inventoryIdentity",System.identityHashCode(call(player,"getInventory")));
                    state.put("containerIdentity",System.identityHashCode(field(player,"containerMenu")));
                }
                if (level != null) state.put("dimension",String.valueOf(call(level,"dimension")));
            }
            states.add(state);
        }
        out.put("sessions",states); return out;
    }
    private static void close(Session s) {
        requireOwner(); activate(s); s.keys.clear(); s.buttons.clear(); s.modifiers = 0;
        try { s.graphics=Map.copyOf(nativeGraphicsSnapshot(s)); }
        catch(ReflectiveOperationException e) { System.err.println("Native handle snapshot failed: "+e); }
        try {
            try { disconnectNetwork(s,"Closing MultiCraft session"); }
            finally { call(s.minecraft,"exitWorldAndClose"); s.nativeCleanupComplete=true; }
        }
        catch (Throwable e) { if (s.failure == null) s.failure = "cleanup: " + describe(rootCause(e)); }
        finally {
            try { if(audioMode.equals("loopback-pcm")) HostAudio.shutdown(s.id); s.audioCleanupComplete=true; }
            catch(Throwable e) { if(s.failure==null) s.failure="audio cleanup: "+describe(rootCause(e)); }
            finally { s.closed = true; WINDOWS.remove(s.handle); }
        }
    }
    private static void finishSdl() throws Exception {
        requireOwner();
        List<String> incomplete=new ArrayList<>();
        for(Session s:SESSIONS.values())
            if(s.minecraft==null || !s.closed || !s.nativeCleanupComplete || !s.audioCleanupComplete || !HostGraphics.sessionReleased(s.id))
                incomplete.add(s.id);
        allNativeResourcesReleased=!SESSIONS.isEmpty() && SESSIONS.size()<=16 && incomplete.isEmpty() && HostGraphics.deferredEmpty();
        if(sdlQuitPolicy.equals("process-exit")) {
            if(!allNativeResourcesReleased) {
                sdlQuitStatus="refused-incomplete-native-cleanup";
                sdlShutdownEvent("process-exit-refused",incomplete);
                saveFinalState();
                // No success acknowledgement or native fallback on a failed cleanup.
                // An uncaught exception preserves a nonzero Java exit for the supervisor.
                throw new IllegalStateException("Process-exit policy requires confirmed native cleanup: "+incomplete);
            }
            sdlQuitStatus="skipped-native-quit-os-process-reclaim";
            sdlShutdownEvent("skip-native-SDL-Quit",incomplete);
            saveFinalState();
            return;
        }
        sdlQuitStatus="native-quit-started"; nativeSdlQuitCalled=true;
        sdlShutdownEvent("native-SDL-Quit-begin",incomplete); saveFinalState();
        SDLInit.SDL_Quit();
        nativeSdlQuitCompleted=true; sdlQuitStatus="native-quit-completed";
        sdlShutdownEvent("native-SDL-Quit-complete",incomplete); saveFinalState();
    }
    private static void saveFinalState() {
        try { writeState(); } catch(Exception e) { System.err.println("Final SDL state write failed: "+e); }
    }
    private static void sdlShutdownEvent(String action,List<String> incomplete) {
        System.err.println(JSON.toJson(Map.of("event","sdl-shutdown","action",action,"policy",sdlQuitPolicy,
            "status",sdlQuitStatus,"allNativeResourcesReleased",allNativeResourcesReleased,"incompleteSessions",incomplete,
            "pid",ProcessHandle.current().pid(),"ownerThreadId",owner.threadId())));
        System.err.flush();
    }
    private static Map<String,Object> nativeGraphicsSnapshot(Session s) throws ReflectiveOperationException {
        Map<String,Object> state=new LinkedHashMap<>(s.graphics);
        if("Vulkan".equals(state.get("backendName"))) {
            Object device=s.loader.loadClass("com.mojang.blaze3d.systems.RenderSystem").getMethod("getDevice").invoke(null);
            Object backend=field(device,"backend"),instance=call(backend,"instance"),vkInstance=call(instance,"vkInstance"),vkDevice=call(backend,"vkDevice");
            Object surfaceBackend=field(field(s.minecraft,"windowSurface"),"backend");
            state.put("nativeInstance",Long.toUnsignedString(((Number)call(vkInstance,"address")).longValue()));
            state.put("nativeDevice",Long.toUnsignedString(((Number)call(vkDevice,"address")).longValue()));
            state.put("nativeSurface",Long.toUnsignedString(((Number)field(surfaceBackend,"surface")).longValue()));
            state.put("nativeSwapchain",Long.toUnsignedString(((Number)field(surfaceBackend,"swapchain")).longValue()));
        }
        return state;
    }
    /** Read the original limiter and native geometry without changing their policy. */
    private static Map<String,Object> framerateSnapshot(Session s) throws ReflectiveOperationException {
        requireOwner();
        Object tracker=field(s.minecraft,"framerateLimitTracker"),options=field(s.minecraft,"options");
        Map<String,Object> state=new LinkedHashMap<>();
        state.put("throttleReason",String.valueOf(call(tracker,"getThrottleReason")));
        state.put("effectiveLimit",call(tracker,"getFramerateLimit"));
        state.put("configuredLimit",field(tracker,"framerateLimit"));
        state.put("heavilyThrottled",call(tracker,"isHeavilyThrottled"));
        state.put("latestInputTime",field(tracker,"latestInputTime"));
        state.put("gameMillis",s.loader.loadClass("net.minecraft.util.Util").getMethod("getMillis").invoke(null));
        for(String name:List.of("inactivityFpsLimit","framerateLimit","enableVsync","fullscreen","exclusiveFullscreen")) {
            Object value=call(call(options,name),"get");
            state.put(name,value instanceof Enum<?> e ? e.name() : value);
        }
        Map<String,Object> window=new LinkedHashMap<>();
        // isFocused() is the existing cloud-seat adaptation; preserve both that
        // result and the untouched native-event field so diagnostics distinguish them.
        window.put("logicalFocused",call(s.window,"isFocused"));
        for(String name:List.of("iconified","focused","fullscreen","fullscreenRequested","exclusiveFullscreen",
                "borderlessFullscreen","width","height","framebufferWidth","framebufferHeight"))
            window.put(name,field(s.window,name));
        window.put("nativeFlags",Long.toUnsignedString(SDLVideo.SDL_GetWindowFlags(s.handle)));
        state.put("window",window);
        return state;
    }
    private static void refreshConnectionEpoch(Session s,Object connection) throws ReflectiveOperationException {
        if(connection!=null) s.networkConnection=call(connection,"getConnection");
        if(!s.connectionObserved) { s.connectionObserved=true; s.lastConnection=connection; return; }
        if(s.lastConnection!=connection) {
            s.connectionEpoch++; s.lastInputSeq=0; s.lastConnection=connection;
            JsonObject release=new JsonObject(); release.addProperty("type","release"); input(s,release);
        }
    }
    private static void disconnectNetwork(Session s,String reason) throws ReflectiveOperationException {
        requireOwner();
        Object screen=call(field(s.minecraft,"gui"),"screen");
        if(screen!=null && screen.getClass().getName().equals("net.minecraft.client.gui.screens.ConnectScreen")) {
            // The pinned original Cancel handler synchronizes its connect worker,
            // aborts a not-yet-established ChannelFuture and closes its connection.
            Class<?> button=s.loader.loadClass("net.minecraft.client.gui.components.Button");
            privateCall(screen,"lambda$init$0",new Class<?>[]{button},(Object)null);
        }
        Set<Object> connections=Collections.newSetFromMap(new IdentityHashMap<>());
        Object listener=call(s.minecraft,"getConnection");
        if(listener!=null) { Object actual=call(listener,"getConnection"); s.networkConnection=actual; connections.add(actual); }
        if(s.networkConnection!=null) connections.add(s.networkConnection);
        Object pending=field(s.minecraft,"pendingConnection"); if(pending!=null) connections.add(pending);
        Class<?> component=s.loader.loadClass("net.minecraft.network.chat.Component");
        Object message=component.getMethod("literal",String.class).invoke(null,reason);
        for(Object connection:connections) {
            // Original Connection.disconnect closes and awaits its own Netty channel;
            // local world cleanup alone does not close the native TCP session.
            call(connection,"disconnect",new Class<?>[]{component},message);
            if(Boolean.TRUE.equals(call(connection,"isConnected"))) throw new IllegalStateException("Native connection remained open after disconnect");
        }
    }
    private static void writeState() throws Exception {
        Path parent = stateFile.toAbsolutePath().getParent(); Files.createDirectories(parent);
        Path temporary = Files.createTempFile(parent,"host-state-",".json");
        try { Files.writeString(temporary,JSON.toJson(snapshot())+"\n"); Files.move(temporary,stateFile,StandardCopyOption.ATOMIC_MOVE,StandardCopyOption.REPLACE_EXISTING); }
        finally { Files.deleteIfExists(temporary); }
    }
    private static void activate(Session s) { requireOwner(); active=s; owner.setContextClassLoader(s.loader); }
    private static void requireOwner() { if (Thread.currentThread()!=owner) throw new IllegalStateException("Native operation outside owner thread"); }
    private static void drainCommands() { int count=128; Runnable r; while(count-->0 && (r=COMMANDS.poll())!=null) r.run(); }
    private static Object field(Object object,String name) throws ReflectiveOperationException {
        for(Class<?> type=object.getClass();type!=null;type=type.getSuperclass()) try { Field f=type.getDeclaredField(name); f.setAccessible(true); return f.get(object); } catch(NoSuchFieldException ignored) {}
        throw new NoSuchFieldException(name);
    }
    private static Object call(Object o,String name) throws ReflectiveOperationException { return call(o,name,new Class<?>[0]); }
    private static Object call(Object o,String name,Class<?>[] types,Object... values) throws ReflectiveOperationException { return o.getClass().getMethod(name,types).invoke(o,values); }
    private static Object privateCall(Object o,String name,Class<?>[] types,Object... values) throws ReflectiveOperationException { Method m=o.getClass().getDeclaredMethod(name,types); m.setAccessible(true); return m.invoke(o,values); }
    private static Throwable rootCause(Throwable e) {
        while((e instanceof InvocationTargetException || e instanceof ExecutionException) && e.getCause()!=null) e=e.getCause();
        return e;
    }
    private static String describe(Throwable e) { return e.getClass().getName()+": "+e.getMessage(); }
    private static String required(JsonObject o,String key) { if(!o.has(key)) throw new IllegalArgumentException("Missing "+key); return o.get(key).getAsString(); }
    private static int integer(JsonObject o,String key) {
        double value=number(o,key);
        if(value!=Math.rint(value) || value<Integer.MIN_VALUE || value>Integer.MAX_VALUE) throw new IllegalArgumentException("Expected integer "+key);
        return (int)value;
    }
    private static long safeSequence(JsonObject o,String key) {
        double value=number(o,key);
        if(value!=Math.rint(value) || value<0 || value>9007199254740991d) throw new IllegalArgumentException("Invalid safe sequence "+key);
        return (long)value;
    }
    private static double number(JsonObject o,String key) { double v=o.get(key).getAsDouble(); if(!Double.isFinite(v)) throw new IllegalArgumentException("Nonfinite input"); return v; }
    private static double optionalNumber(JsonObject o,String key) { return o.has(key)?number(o,key):0; }
    private static String argument(String[] args,String key) {
        String value=null;
        for(int i=0;i<args.length;i++) if(args[i].equals(key)) {
            if(value!=null || i+1>=args.length) throw new IllegalArgumentException("Duplicate or missing "+key);
            value=args[++i];
        }
        if(value==null) throw new IllegalArgumentException("Missing per-session "+key);
        return value;
    }
    private static Path gameDirectory(String[] args) { return Path.of(argument(args,"--gameDir")); }
    private static String sha256(Path path) throws Exception { MessageDigest hash=MessageDigest.getInstance("SHA-256"); try(var in=Files.newInputStream(path)){ byte[] buffer=new byte[131072]; int n; while((n=in.read(buffer))!=-1) hash.update(buffer,0,n); } return HexFormat.of().formatHex(hash.digest()); }
    private static void respond(HttpExchange ex,int status,Object body) throws IOException { byte[] bytes=JSON.toJson(body).getBytes(StandardCharsets.UTF_8); ex.getResponseHeaders().set("Content-Type","application/json; charset=utf-8"); ex.getResponseHeaders().set("Cache-Control","no-store"); ex.sendResponseHeaders(status,bytes.length); ex.getResponseBody().write(bytes); }
}
