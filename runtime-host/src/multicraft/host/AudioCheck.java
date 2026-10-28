package multicraft.host;

import org.lwjgl.openal.*;
import org.lwjgl.system.MemoryUtil;
import java.nio.*;
import java.util.*;

/** Isolated synthetic native check of the host audio adapter; never Minecraft audio evidence. */
public final class AudioCheck {
    private static int tone(String id,int hz) {
        if(!HostAudio.enter(id,null,0)) throw new AssertionError();
        try {
            long device=HostAudio.openLoopback(id);
            var alc=HostAudio.createAlcCapabilities(device,id);
            long context=HostAudio.createContext(device,null,id);
            if(!HostAudio.makeContextCurrent(context,id)) throw new AssertionError("Context bind failed");
            HostAudio.createAlCapabilities(alc,id);
            int source=AL10.alGenSources(), buffer=AL10.alGenBuffers();
            ShortBuffer samples=MemoryUtil.memAllocShort(48000*4);
            try {
                for(int i=0;i<48000*4;i++) samples.put((short)(Math.sin(2*Math.PI*hz*i/48000)*12000));
                samples.flip(); AL10.alBufferData(buffer,AL10.AL_FORMAT_MONO16,samples,48000);
            } finally { MemoryUtil.memFree(samples); }
            AL10.alSourcei(source,AL10.AL_BUFFER,buffer); AL10.alSourcei(source,AL10.AL_LOOPING,AL10.AL_TRUE);
            AL10.alSourcei(source,AL10.AL_SOURCE_RELATIVE,AL10.AL_TRUE); AL10.alSourcePlay(source);
            if(AL10.alGetError()!=AL10.AL_NO_ERROR) throw new AssertionError("Native AL error");
            return source;
        } finally { HostAudio.exit(id); }
    }
    private static int offset(String id,int source) {
        if(!HostAudio.enter(id,null,0)) throw new AssertionError();
        try { return AL10.alGetSourcei(source,AL11.AL_SAMPLE_OFFSET); }
        finally { HostAudio.exit(id); }
    }
    private static Map<String,Object> measure(String id,int expected) {
        HostAudio.Chunk chunk=HostAudio.read(id,-1,0,4800);
        ByteBuffer pcm=ByteBuffer.wrap(chunk.data()).order(ByteOrder.LITTLE_ENDIAN);
        int crossings=0, previous=0; double squared=0;
        for(int i=0;i<pcm.limit()/4;i++) {
            int sample=pcm.getShort(i*4); if(i>0 && previous<=0 && sample>0) crossings++;
            previous=sample; squared+=(double)sample*sample;
        }
        double seconds=chunk.data().length/4.0/48000, frequency=0, strongest=-1;
        // OpenAL's initial fade/dither can create tiny zero crossings; measure the spectral peak.
        for(int candidate=100;candidate<=1000;candidate+=10) {
            double real=0,imaginary=0;
            for(int i=0;i<pcm.limit()/4;i++) { double phase=2*Math.PI*candidate*i/48000; int value=pcm.getShort(i*4); real+=value*Math.cos(phase); imaginary+=value*Math.sin(phase); }
            double energy=real*real+imaginary*imaginary;
            if(energy>strongest) { strongest=energy; frequency=candidate; }
        }
        if(Math.abs(frequency-expected)>15 || squared==0) throw new AssertionError("Unexpected PCM "+id+": "+frequency);
        return Map.of("id",id,"epoch",chunk.epoch(),"frames",chunk.endFrame()-chunk.startFrame(),"frequency",frequency,"expected",expected);
    }
    public static void main(String[] args) throws Exception {
        HostAudio.register("native-check-A"); HostAudio.register("native-check-B");
        try {
            int sourceA=tone("native-check-A",220); tone("native-check-B",880);
            if(ALC10.alcGetCurrentContext()!=0) throw new AssertionError("Process-global context must remain null");
            Object old=new Object(); HostAudio.tag("native-check-A",old);
            Thread.sleep(350);
            System.out.println(measure("native-check-A",220)); System.out.println(measure("native-check-B",880));
            int beforeOffset=offset("native-check-A",sourceA);
            long beforeWall=System.nanoTime();
            if(!HostAudio.enter("native-check-A",null,0)) throw new AssertionError();
            try { Thread.sleep(500); } finally { HostAudio.exit("native-check-A"); }
            Thread.sleep(140);
            int advanced=offset("native-check-A",sourceA)-beforeOffset;
            long expected=(System.nanoTime()-beforeWall)*48000/1_000_000_000L;
            if(Math.abs(advanced-expected)>4800) throw new AssertionError("Native playhead did not catch up: advanced="+advanced+", wallFrames="+expected);
            System.out.println(Map.of("check","native-source-playhead-after-500ms-stall","advancedFrames",advanced,"wallFrames",expected,
                "discardedNativeFrames",HostAudio.state("native-check-A").get("clockGapFrames")));
            var gap=HostAudio.read("native-check-A",1,0,4800);
            if(gap.droppedFrames()==0 || gap.startFrame()==0 || ((Number)HostAudio.state("native-check-A").get("clockDiscontinuities")).longValue()==0)
                throw new AssertionError("Clock stall did not expose a transport discontinuity");
            long beforeLongStall=((Number)HostAudio.state("native-check-B").get("producedFrames")).longValue();
            if(!HostAudio.enter("native-check-A",null,0)) throw new AssertionError();
            try { Thread.sleep(2150); } finally { HostAudio.exit("native-check-A"); }
            Thread.sleep(80);
            if(!Boolean.TRUE.equals(HostAudio.state("native-check-A").get("clockResetRequired"))) throw new AssertionError("Oversized stall did not request explicit reset");
            long afterLongStall=((Number)HostAudio.state("native-check-B").get("producedFrames")).longValue();
            if(afterLongStall<=beforeLongStall) throw new AssertionError("A oversized stall blocked B");
            System.out.println(Map.of("check","oversized-stall-explicit-reset","clockResetRequired",true,"peerAdvancedFrames",afterLongStall-beforeLongStall));
            long before=((Number)HostAudio.state("native-check-B").get("producedFrames")).longValue();
            if(!HostAudio.enter("native-check-A",null,0)) throw new AssertionError();
            try {
                var state=HostAudio.state("native-check-A");
                HostAudio.destroyContext(Long.parseUnsignedLong((String)state.get("context")),"native-check-A");
                HostAudio.closeDevice(Long.parseUnsignedLong((String)state.get("device")),"native-check-A");
            } finally { HostAudio.exit("native-check-A"); }
            Thread.sleep(120);
            long after=((Number)HostAudio.state("native-check-B").get("producedFrames")).longValue();
            if(after<=before) throw new AssertionError("Closing A interrupted B sampling");
            tone("native-check-A",440);
            if(Boolean.TRUE.equals(HostAudio.state("native-check-A").get("clockResetRequired"))) throw new AssertionError("New epoch did not reset clock failure");
            if(HostAudio.enter("native-check-A",old,0)) { HostAudio.exit("native-check-A"); throw new AssertionError("Stale resource accepted"); }
            Thread.sleep(150); System.out.println(measure("native-check-A",440));
            System.out.println("NATIVE SYNTHETIC CHECK ONLY: two isolated PCM streams, A teardown/reinit preserves B, stale epoch rejected. Minecraft sound not tested.");
        } finally { HostAudio.shutdown("native-check-A"); HostAudio.shutdown("native-check-B"); }
        for(String id:List.of("native-check-A","native-check-B")) {
            Map<String,Object> state=HostAudio.state(id);
            if(!"0".equals(state.get("device")) || !"0".equals(state.get("context"))
                    || !Boolean.TRUE.equals(state.get("terminated")) || Boolean.TRUE.equals(state.get("samplerAlive")))
                throw new AssertionError("Native audio shutdown was not confirmed: "+state);
        }
        System.out.println("NATIVE AUDIO CLEANUP CONFIRMED: both devices/context handles cleared and samplers stopped.");
    }
}
