package me.Integer.localwhisper

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Test

class ServerDiscoveryTest {

    private fun ip(a: Int, b: Int, c: Int, d: Int) = byteArrayOf(a.toByte(), b.toByte(), c.toByte(), d.toByte())

    private fun parse(json: String, sender: String = "192.168.1.20"): DiscoveredServer? {
        val bytes = json.toByteArray()
        return DiscoveryReplyParser.parse(bytes, bytes.size, sender)
    }

    @Test
    fun parsesValidReply() {
        val server = parse("""{"service":"localwhisper","version":1,"name":"GamingPC","port":8765,"asr_backend":"llm"}""")
        assertEquals(DiscoveredServer("192.168.1.20", 8765, "GamingPC", "llm"), server)
    }

    @Test
    fun rejectsOtherServicesBadPortsAndGarbage() {
        assertNull(parse("""{"service":"other","port":8765}"""))
        assertNull(parse("""{"service":"localwhisper","port":0}"""))
        assertNull(parse("""{"service":"localwhisper","port":70000}"""))
        assertNull(parse("not json"))
        assertNull(parse("""{"service":"localwhisper","port":8765}""", sender = ""))
    }

    @Test
    fun blankNameFallsBackToHost() {
        val server = parse("""{"service":"localwhisper","name":"  ","port":8765}""")
        assertEquals("192.168.1.20", server?.name)
    }

    @Test
    fun healthMustLookLikeLocalWhisper() {
        val good = """{"status":"ok","server_name":"Desk","connected_clients":[]}"""
        assertEquals("Desk", DiscoveryReplyParser.parseHealth(good, "10.0.0.5", 8765)?.name)
        assertNull(DiscoveryReplyParser.parseHealth("""{"status":"ok"}""", "10.0.0.5", 8765))
        assertNull(DiscoveryReplyParser.parseHealth(good, "10.0.0.5", 0))
    }

    @Test
    fun broadcastAddressMath() {
        assertArrayEquals(ip(192, 168, 1, 255), SubnetMath.broadcastAddress(ip(192, 168, 1, 37), 24))
        assertArrayEquals(ip(10, 0, 7, 255), SubnetMath.broadcastAddress(ip(10, 0, 5, 9), 22))
        assertNull(SubnetMath.broadcastAddress(ip(10, 0, 5, 9), 0))
        assertNull(SubnetMath.broadcastAddress(ip(10, 0, 5, 9), 32))
    }

    @Test
    fun sweepHostsCoverTheSlash24WithoutSelf() {
        val hosts = SubnetMath.sweepHosts(ip(192, 168, 1, 37))
        assertEquals(253, hosts.size)
        assertEquals("192.168.1.1", hosts.first())
        assertEquals("192.168.1.254", hosts.last())
        assertFalse(hosts.contains("192.168.1.37"))
    }
}
