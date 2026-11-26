package me.Integer.localwhisper

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

class NotificationStoreTest {

    @Before
    fun setUp() {
        NotificationStore.clear()
    }

    @Test
    fun allowlistRejectsOwnPackageAndUnlistedApps() {
        val allowed = setOf("com.whatsapp", "com.Slack")
        assertTrue(NotificationStore.shouldAcceptPackage("com.whatsapp", allowed))
        assertTrue(NotificationStore.shouldAcceptPackage("com.slack", allowed))
        assertFalse(NotificationStore.shouldAcceptPackage("me.Integer.localwhisper", allowed))
        assertFalse(NotificationStore.shouldAcceptPackage("com.bank.secret", allowed))
    }

    @Test
    fun upsertsInPlaceForSameSbnKeyAndFiltersByAppAndText() {
        val t0 = 1_000_000L
        val first = NotificationStore.upsert(
            sbnKey = "key_wa_1",
            packageName = "com.whatsapp",
            appName = "WhatsApp",
            title = "Alice",
            text = "Are you free?",
            messages = listOf("Alice: Are you free?"),
            postedAtMs = t0,
            canReply = true,
            nowMs = t0
        )
        assertNotNull(first)
        assertEquals("n1", first!!.id)

        val updated = NotificationStore.upsert(
            sbnKey = "key_wa_1",
            packageName = "com.whatsapp",
            appName = "WhatsApp",
            title = "Alice",
            text = "Let's meet at 3",
            messages = listOf("Alice: Are you free?", "Alice: Let's meet at 3"),
            postedAtMs = t0 + 5_000L,
            canReply = true,
            nowMs = t0 + 5_000L
        )
        assertEquals("n1", updated!!.id)
        assertEquals(1, NotificationStore.list(nowMs = t0 + 5_000L).size)

        NotificationStore.upsert(
            sbnKey = "key_slack_1",
            packageName = "com.Slack",
            appName = "Slack",
            title = "#deploy",
            text = "Build green",
            messages = emptyList(),
            postedAtMs = t0 + 10_000L,
            canReply = false,
            nowMs = t0 + 10_000L
        )

        val slackOnly = NotificationStore.list(appQuery = "slack", nowMs = t0 + 10_000L)
        assertEquals(1, slackOnly.size)
        assertEquals("n2", slackOnly[0].id)

        val searchHit = NotificationStore.list(search = "meet at 3", nowMs = t0 + 10_000L)
        assertEquals(1, searchHit.size)
        assertEquals("n1", searchHit[0].id)
    }

    @Test
    fun expiresOldNotificationsAfterTtl() {
        val t0 = 5_000_000L
        NotificationStore.upsert(
            sbnKey = "key_old",
            packageName = "com.whatsapp",
            appName = "WhatsApp",
            title = "Bob",
            text = "Yesterday",
            messages = emptyList(),
            postedAtMs = t0,
            canReply = true,
            nowMs = t0
        )
        assertNull(NotificationStore.getById("n1", nowMs = t0 + NotificationStore.TTL_MS + 1000L))
    }
}
