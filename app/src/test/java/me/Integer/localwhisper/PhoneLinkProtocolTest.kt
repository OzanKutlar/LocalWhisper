package me.Integer.localwhisper

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class PhoneLinkProtocolTest {

    @Test
    fun buildsWsUrlFromHttpBase() {
        val url = PhoneLinkProtocol.buildWsUrl("http://192.168.1.20:8765", "Pixel 8 Pro")
        assertEquals("ws://192.168.1.20:8765/ws/phone?name=Pixel+8+Pro", url)
        assertNull(PhoneLinkProtocol.buildWsUrl("   ", "Pixel"))
    }

    @Test
    fun buildsResultAndEventFrames() {
        val okResult = JSONObject().put("ok", true).put("battery_pct", 85)
        val okFrame = JSONObject(PhoneLinkProtocol.buildResultFrame("req1", okResult))
        assertEquals("result", okFrame.getString("type"))
        assertEquals("req1", okFrame.getString("id"))
        assertTrue(okFrame.getBoolean("ok"))
        assertEquals(85, okFrame.getJSONObject("result").getInt("battery_pct"))

        val errResult = JSONObject().put("ok", false).put("error", "phone_locked")
        val errFrame = JSONObject(PhoneLinkProtocol.buildResultFrame("req2", errResult))
        assertFalse(errFrame.getBoolean("ok"))
        assertEquals("phone_locked", errFrame.getString("error"))

        val eventFrame = JSONObject(PhoneLinkProtocol.buildEventFrame("remote_stop", JSONObject().put("by", "user")))
        assertEquals("event", eventFrame.getString("type"))
        assertEquals("remote_stop", eventFrame.getString("event"))
        assertEquals("user", eventFrame.getJSONObject("data").getString("by"))
    }
}
