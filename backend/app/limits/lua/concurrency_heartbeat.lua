local redis_time = redis.call("TIME")
local now_ms = (tonumber(redis_time[1]) * 1000) + math.floor(tonumber(redis_time[2]) / 1000)
local lease_ms = tonumber(ARGV[1])
local member = ARGV[2]
local existing_expiry = redis.call("ZSCORE", KEYS[1], member)

if not existing_expiry or tonumber(existing_expiry) <= now_ms then
    redis.call("ZREM", KEYS[1], member)
    return {0, now_ms, now_ms}
end

local expires_at_ms = now_ms + lease_ms
redis.call("ZADD", KEYS[1], "XX", expires_at_ms, member)
redis.call("PEXPIRE", KEYS[1], lease_ms)
return {1, expires_at_ms, now_ms}
