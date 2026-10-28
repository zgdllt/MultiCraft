package multicraft.host;

import org.lwjgl.openal.*;
import org.lwjgl.system.MemoryStack;
import org.lwjgl.system.MemoryUtil;

import java.lang.reflect.Proxy;
import java.nio.*;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.locks.*;

/** Per-realm loopback devices, explicit context scopes and a bounded PCM history. */
public final class HostAudio {
    public static final int SAMPLE_RATE=48000, CHANNELS=2, FRAME_BYTES=4, BLOCK_FRAMES=960;
    private static final int CAPACITY=96000;
    private static final Map<String,AudioSession> SESSIONS=new ConcurrentHashMap<>();
    private static final class AudioSession {
        final String id;
        final ReentrantLock lock=new ReentrantLock();
        final WeakHashMap<Object,Long> resourceEpochs=new WeakHashMap<>();
        final byte[] pcm=new byte[CAPACITY*FRAME_BYTES];
        volatile long device,context,epoch,staleCalls;
        volatile String error;
        volatile boolean retired;
        volatile boolean terminated;
        volatile boolean clockResetRequired;
        ALCCapabilities alc;
        ALCapabilities al;
        volatile Thread sampler;
        long producedFrames,nonzeroSamples,lateBlocks,minimumFrame,clockGapFrames,clockDiscontinuities;
        double lastRms;
        AudioSession(String id) { this.id=id; }
    }
    public record Chunk(long epoch,long startFrame,long endFrame,long droppedFrames,byte[] data) {}
    private HostAudio() {}
    public static void register(String id) {
        if(SESSIONS.putIfAbsent(id,new AudioSession(id))!=null) throw new IllegalStateException("Duplicate audio session");
    }
    private static AudioSession session(String id) {
        AudioSession s=SESSIONS.get(id); if(s==null) throw new IllegalStateException("Unknown audio session"); return s;
    }
    /** A scope holds the lifecycle lock until exit(), including exceptional exits. */
    public static boolean enter(String id,Object resource,int reserved) {
        AudioSession s=session(id); s.lock.lock();
        try {
            if(s.terminated) { s.staleCalls++; s.lock.unlock(); return false; }
            if(resource!=null) {
                Long old=s.resourceEpochs.get(resource);
                if(s.context==0 || s.retired || (old!=null && old!=s.epoch)) {
                    s.staleCalls++; s.lock.unlock(); return false;
                }
                s.resourceEpochs.putIfAbsent(resource,s.epoch);
            }
            bind(s); return true;
        } catch(Throwable e) { s.lock.unlock(); throw e; }
    }
    public static void tag(String id,Object resource) {
        AudioSession s=session(id); s.lock.lock();
        try { if(s.context!=0 && !s.retired) s.resourceEpochs.put(resource,s.epoch); }
        finally { s.lock.unlock(); }
    }
    public static void rebind(String id) {
        AudioSession s=session(id);
        if(!s.lock.isHeldByCurrentThread()) throw new IllegalStateException("Unscoped OpenAL call for "+id);
        bind(s);
    }
    public static void exit(String id) {
        AudioSession s=session(id);
        // Exit must not throw into the transformed catch-all and unlock twice.
        if(!s.lock.isHeldByCurrentThread()) return;
        try {
            if(s.lock.getHoldCount()==1 && s.alc!=null) {
                ALC.setCapabilities(s.alc);
                EXTThreadLocalContext.alcSetThreadContext(0);
                AL.setCurrentThread(null);
            }
        } catch(Throwable e) { s.error="unbind: "+e; }
        finally { s.lock.unlock(); }
    }
    private static void bind(AudioSession s) {
        if(s.alc!=null) ALC.setCapabilities(s.alc);
        if(s.context!=0 && !s.retired) {
            if(!EXTThreadLocalContext.alcSetThreadContext(s.context)) throw new IllegalStateException("Cannot bind audio "+s.id);
            AL.setCurrentThread(s.al);
        } else if(s.alc!=null) {
            EXTThreadLocalContext.alcSetThreadContext(0); AL.setCurrentThread(null);
        }
    }
    public static long openLoopback(String id) {
        AudioSession s=session(id); s.lock.lock();
        try {
            if(s.device!=0) throw new IllegalStateException("Previous audio device was not cleaned up");
            if(s.terminated) throw new IllegalStateException("Audio session has closed");
            if(!ALC.getCapabilities().ALC_SOFT_loopback) throw new IllegalStateException("ALC_SOFT_loopback unavailable");
            long device=SOFTLoopback.alcLoopbackOpenDeviceSOFT((ByteBuffer)null);
            if(device==0) throw new IllegalStateException("Cannot open independent loopback audio device");
            s.device=device; s.retired=false; s.context=0; s.al=null; s.error=null; s.clockResetRequired=false;
            synchronized(s.pcm) { s.epoch++; s.producedFrames=0; s.minimumFrame=0; s.clockGapFrames=0; s.clockDiscontinuities=0;
                s.nonzeroSamples=0; s.lastRms=0; Arrays.fill(s.pcm,(byte)0); }
            return device;
        } finally { s.lock.unlock(); }
    }
    public static ALCCapabilities createAlcCapabilities(long device,String id) {
        AudioSession s=session(id);
        if(device!=s.device) throw new IllegalArgumentException("Device does not belong to realm");
        ALCCapabilities caps=ALC.createCapabilities(device);
        if(!caps.ALC_SOFT_loopback || !caps.ALC_EXT_thread_local_context) throw new IllegalStateException("Required loopback/thread-local extensions unavailable");
        s.alc=caps; ALC.setCapabilities(caps); return caps;
    }
    public static long createContext(long device,IntBuffer original,String id) {
        AudioSession s=session(id);
        if(device!=s.device) throw new IllegalArgumentException("Device does not belong to realm");
        // Retain native source-count/HRTF attributes while pinning PCM transport format.
        List<Integer> values=new ArrayList<>();
        if(original!=null) for(int i=original.position();i<original.limit()-1;i+=2) {
            int key=original.get(i); if(key==0) break;
            if(key!=0x1007 && key!=0x1990 && key!=0x1991) { values.add(key); values.add(original.get(i+1)); }
        }
        values.addAll(List.of(0x1990,0x1501,0x1991,0x1402,0x1007,SAMPLE_RATE,0));
        try(MemoryStack stack=MemoryStack.stackPush()) {
            IntBuffer attributes=stack.mallocInt(values.size()); for(int value:values) attributes.put(value); attributes.flip();
            long context=ALC10.alcCreateContext(device,attributes);
            if(context==0) throw new IllegalStateException("Cannot create loopback context; ALC error="+ALC10.alcGetError(device));
            s.context=context; return context;
        }
    }
    public static boolean makeContextCurrent(long context,String id) {
        AudioSession s=session(id);
        if(context!=0 && context!=s.context) throw new IllegalArgumentException("Context does not belong to realm");
        ALC.setCapabilities(s.alc);
        return EXTThreadLocalContext.alcSetThreadContext(context);
    }
    public static ALCapabilities createAlCapabilities(ALCCapabilities alc,String id) {
        AudioSession s=session(id); s.al=AL.createCapabilities(alc); startSampler(s); return s.al;
    }
    public static void destroyContext(long context,String id) {
        AudioSession s=session(id);
        if(context==0) return;
        if(context!=s.context || !s.lock.isHeldByCurrentThread()) throw new IllegalStateException("Unowned context teardown");
        s.retired=true;
        EXTThreadLocalContext.alcSetThreadContext(0); AL.setCurrentThread(null);
        ALC10.alcDestroyContext(context); s.context=0; s.al=null;
    }
    public static boolean closeDevice(long device,String id) {
        AudioSession s=session(id);
        if(device==0) return true;
        if(device!=s.device || !s.lock.isHeldByCurrentThread()) throw new IllegalStateException("Unowned device teardown");
        boolean result=ALC10.alcCloseDevice(device);
        if(result) s.device=0;
        else s.error="Native alcCloseDevice refused device "+Long.toUnsignedString(device);
        return result;
    }
    private static void startSampler(AudioSession s) {
        long epoch=s.epoch;
        Thread thread=new Thread(()->sample(s,epoch),"MultiCraft audio "+s.id+" epoch "+epoch);
        thread.setDaemon(true); s.sampler=thread; thread.start();
    }
    private static void sample(AudioSession s,long epoch) {
        ByteBuffer buffer=MemoryUtil.memAlloc(BLOCK_FRAMES*FRAME_BYTES).order(ByteOrder.LITTLE_ENDIAN);
        long next=System.nanoTime();
        boolean catchingUp=false;
        try {
            while(s.epoch==epoch && !s.retired) {
                long wait=next-System.nanoTime();
                if(wait>0) { LockSupport.parkNanos(wait); continue; }
                if(!s.lock.tryLock()) { LockSupport.parkNanos(200_000); continue; }
                boolean discarded=false;
                try {
                    if(s.epoch!=epoch || s.retired || s.context==0) break;
                    bind(s);
                    long lag=System.nanoTime()-next;
                    if(lag>2_000_000_000L) {
                        s.clockResetRequired=true;
                        s.error="Audio clock stall exceeded 2 seconds; reload resources to create a new epoch";
                        synchronized(s.pcm) { s.minimumFrame=s.producedFrames; }
                        break;
                    }
                    if(lag>100_000_000L && !catchingUp) {
                        catchingUp=true;
                        synchronized(s.pcm) { s.clockDiscontinuities++; s.lateBlocks++; }
                    }
                    if(catchingUp && lag>=20_000_000L) {
                        // Advancing only packet timestamps would leave the actual source
                        // playhead behind. Render and discard missed native samples too.
                        // At most 100 ms of audio is mixed per lock acquisition.
                        int blocks=(int)Math.min(5,lag/20_000_000L);
                        for(int i=0;i<blocks;i++) { render(s,buffer); next+=20_000_000L; }
                        synchronized(s.pcm) {
                            long frames=(long)blocks*BLOCK_FRAMES;
                            s.producedFrames+=frames; s.minimumFrame=s.producedFrames; s.clockGapFrames+=frames;
                        }
                        discarded=true;
                    } else {
                        catchingUp=false; render(s,buffer); append(s,buffer); next+=20_000_000L;
                    }
                } finally { exit(s.id); }
                if(discarded) LockSupport.parkNanos(100_000); // Allow lifecycle and source updates between bounded bursts.
            }
        } catch(Throwable e) { s.error="sampler: "+e; }
        finally { MemoryUtil.memFree(buffer); }
    }
    private static void render(AudioSession s,ByteBuffer buffer) {
        buffer.clear(); SOFTLoopback.alcRenderSamplesSOFT(s.device,buffer,BLOCK_FRAMES);
        int error=ALC10.alcGetError(s.device);
        if(error!=ALC10.ALC_NO_ERROR) throw new IllegalStateException("Loopback render ALC error "+error);
    }
    private static void append(AudioSession s,ByteBuffer bytes) {
        double squared=0; long nonzero=0;
        for(int i=0;i<BLOCK_FRAMES*CHANNELS;i++) { int value=bytes.getShort(i*2); squared+=(double)value*value; if(value!=0) nonzero++; }
        synchronized(s.pcm) {
            int offset=(int)(s.producedFrames%CAPACITY)*FRAME_BYTES;
            int length=BLOCK_FRAMES*FRAME_BYTES;
            for(int i=0;i<length;i++) s.pcm[(offset+i)%s.pcm.length]=bytes.get(i);
            s.producedFrames+=BLOCK_FRAMES; s.nonzeroSamples+=nonzero; s.lastRms=Math.sqrt(squared/(BLOCK_FRAMES*CHANNELS));
        }
    }
    public static Chunk read(String id,long requestedEpoch,long sinceFrame,int maxFrames) {
        AudioSession s=session(id);
        if(sinceFrame<0 || maxFrames<1 || maxFrames>4800) throw new IllegalArgumentException("Invalid audio range");
        synchronized(s.pcm) {
            long earliest=Math.max(s.minimumFrame,s.producedFrames-CAPACITY);
            long wanted=requestedEpoch<0 || requestedEpoch==s.epoch ? sinceFrame : earliest;
            long start=Math.min(s.producedFrames,Math.max(earliest,wanted));
            long end=Math.min(s.producedFrames,start+maxFrames);
            byte[] data=new byte[(int)(end-start)*FRAME_BYTES];
            for(int i=0;i<data.length;i++) data[i]=s.pcm[(int)((start*FRAME_BYTES+i)%s.pcm.length)];
            return new Chunk(s.epoch,start,end,Math.max(0,earliest-wanted),data);
        }
    }
    public static Map<String,Object> state(String id) {
        AudioSession s=session(id); Map<String,Object> state=new LinkedHashMap<>();
        state.put("epoch",s.epoch); state.put("active",s.context!=0 && !s.retired && !s.clockResetRequired); state.put("device",Long.toUnsignedString(s.device));
        state.put("context",Long.toUnsignedString(s.context)); state.put("sampleRate",SAMPLE_RATE); state.put("channels",CHANNELS);
        state.put("format","s16le"); state.put("staleCalls",s.staleCalls); state.put("error",s.error); state.put("lateBlocks",s.lateBlocks);
        state.put("clockResetRequired",s.clockResetRequired);
        state.put("terminated",s.terminated); state.put("samplerAlive",s.sampler!=null && s.sampler.isAlive());
        synchronized(s.pcm) { state.put("producedFrames",s.producedFrames); state.put("earliestFrame",Math.max(s.minimumFrame,s.producedFrames-CAPACITY));
            state.put("renderedFrames",s.producedFrames); state.put("deliveredFrames",s.producedFrames-s.clockGapFrames);
            state.put("clockGapFrames",s.clockGapFrames); state.put("clockDiscontinuities",s.clockDiscontinuities);
            state.put("nonzeroSamples",s.nonzeroSamples); state.put("lastRms",s.lastRms); }
        return state;
    }
    public static void shutdown(String id) {
        AudioSession s=session(id); s.lock.lock();
        try {
            s.terminated=true; s.retired=true;
            if(s.context!=0) destroyContext(s.context,id);
            if(s.device!=0 && !closeDevice(s.device,id)) throw new IllegalStateException(s.error);
        } finally {
            s.lock.unlock();
            Thread sampler=s.sampler;
            if(sampler!=null) {
                if(sampler==Thread.currentThread()) throw new IllegalStateException("Audio sampler cannot acknowledge its own shutdown");
                try { sampler.join(1000); }
                catch(InterruptedException e) {
                    Thread.currentThread().interrupt(); s.error="Interrupted while waiting for audio sampler shutdown";
                    throw new IllegalStateException(s.error,e);
                }
                if(sampler.isAlive()) {
                    s.error="Audio sampler did not stop within the shutdown deadline";
                    throw new IllegalStateException(s.error);
                }
            }
        }
    }
    public static Object fixedDeviceList(Class<?> type) {
        try { return type.getConstructor(String.class,List.class).newInstance("MultiCraft loopback",List.of("MultiCraft loopback")); }
        catch(ReflectiveOperationException e) { throw new IllegalStateException(e); }
    }
    public static Object fixedDeviceTracker(Class<?> type) {
        try {
            Object list=fixedDeviceList(type.getClassLoader().loadClass("com.mojang.blaze3d.audio.DeviceList"));
            return Proxy.newProxyInstance(type.getClassLoader(),new Class<?>[]{type},(proxy,method,args)->switch(method.getName()) {
                case "currentDevices" -> list;
                case "tick","forceRefresh" -> null;
                case "toString" -> "MultiCraft fixed loopback device tracker";
                case "hashCode" -> System.identityHashCode(proxy);
                case "equals" -> proxy==args[0];
                default -> throw new UnsupportedOperationException(method.getName());
            });
        } catch(ClassNotFoundException e) { throw new IllegalStateException(e); }
    }
}
