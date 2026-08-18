local redis_time = redis.call("TIME")
local now_ms = (tonumber(redis_time[1]) * 1000) + math.floor(tonumber(redis_time[2]) / 1000)
local lease_ms = tonumber(ARGV[1])
local active_limit = tonumber(ARGV[2])
local queue_wait_ms = tonumber(ARGV[3])
local queue_limit = tonumber(ARGV[4])
local caller_queue_limit = tonumber(ARGV[5])
local member = ARGV[6]

local function remove_expired_pool_tickets()
    local expired = redis.call("ZRANGEBYSCORE", KEYS[3], "-inf", now_ms)
    for _, expired_member in ipairs(expired) do
        redis.call("ZREM", KEYS[2], expired_member)
        redis.call("ZREM", KEYS[3], expired_member)
    end
end

local function delete_empty_zset(key)
    if redis.call("ZCARD", key) == 0 then
        redis.call("DEL", key)
    end
end

local function refresh_queue_ttls()
    local ttl_ms = queue_wait_ms + 1000
    redis.call("PEXPIRE", KEYS[2], ttl_ms)
    redis.call("PEXPIRE", KEYS[3], ttl_ms)
    redis.call("PEXPIRE", KEYS[4], ttl_ms)
    redis.call("PEXPIRE", KEYS[5], ttl_ms)
    redis.call("PEXPIRE", KEYS[6], ttl_ms)
end

local function retry_at(default_retry_at, zset_key)
    local earliest = redis.call("ZRANGE", zset_key, 0, 0, "WITHSCORES")
    if #earliest >= 2 and tonumber(earliest[2]) < default_retry_at then
        return math.floor(tonumber(earliest[2]))
    end
    return default_retry_at
end

redis.call("ZREMRANGEBYSCORE", KEYS[1], "-inf", now_ms)
remove_expired_pool_tickets()
redis.call("ZREMRANGEBYSCORE", KEYS[4], "-inf", now_ms)
redis.call("ZREMRANGEBYSCORE", KEYS[5], "-inf", now_ms)

local active_count = redis.call("ZCARD", KEYS[1])
local queue_count = redis.call("ZCARD", KEYS[4])
local pool_queue_count = redis.call("ZCARD", KEYS[2])
local existing_active_expiry = redis.call("ZSCORE", KEYS[1], member)

if existing_active_expiry then
    redis.call("ZREM", KEYS[2], member)
    redis.call("ZREM", KEYS[3], member)
    redis.call("ZREM", KEYS[4], member)
    redis.call("ZREM", KEYS[5], member)
    queue_count = redis.call("ZCARD", KEYS[4])
    pool_queue_count = redis.call("ZCARD", KEYS[2])
    redis.call("PEXPIRE", KEYS[1], lease_ms)
    return {1, active_count, queue_count, pool_queue_count, 0, math.floor(tonumber(existing_active_expiry)), now_ms}
end

local existing_ticket_expiry = redis.call("ZSCORE", KEYS[3], member)
if existing_ticket_expiry then
    local rank = redis.call("ZRANK", KEYS[2], member)
    if rank == 0 and active_count < active_limit then
        redis.call("ZREM", KEYS[2], member)
        redis.call("ZREM", KEYS[3], member)
        redis.call("ZREM", KEYS[4], member)
        redis.call("ZREM", KEYS[5], member)
        local active_expires_at = now_ms + lease_ms
        redis.call("ZADD", KEYS[1], "NX", active_expires_at, member)
        redis.call("PEXPIRE", KEYS[1], lease_ms)
        active_count = redis.call("ZCARD", KEYS[1])
        queue_count = redis.call("ZCARD", KEYS[4])
        pool_queue_count = redis.call("ZCARD", KEYS[2])
        delete_empty_zset(KEYS[2])
        delete_empty_zset(KEYS[3])
        delete_empty_zset(KEYS[4])
        delete_empty_zset(KEYS[5])
        return {1, active_count, queue_count, pool_queue_count, 0, active_expires_at, now_ms}
    end
    if rank then
        refresh_queue_ttls()
        return {
            2,
            active_count,
            queue_count,
            pool_queue_count,
            rank + 1,
            math.floor(tonumber(existing_ticket_expiry)),
            now_ms
        }
    end
end

if active_count < active_limit and pool_queue_count == 0 then
    local active_expires_at = now_ms + lease_ms
    redis.call("ZADD", KEYS[1], "NX", active_expires_at, member)
    redis.call("PEXPIRE", KEYS[1], lease_ms)
    active_count = redis.call("ZCARD", KEYS[1])
    return {1, active_count, queue_count, pool_queue_count, 0, active_expires_at, now_ms}
end

local retry_at_ms = now_ms + queue_wait_ms
retry_at_ms = retry_at(retry_at_ms, KEYS[1])
if queue_limit == 0 or queue_count >= queue_limit then
    retry_at_ms = retry_at(retry_at_ms, KEYS[4])
    return {4, active_count, queue_count, pool_queue_count, 0, retry_at_ms, now_ms}
end

local caller_queue_count = redis.call("ZCARD", KEYS[5])
if caller_queue_count >= caller_queue_limit then
    retry_at_ms = retry_at(retry_at_ms, KEYS[5])
    return {3, active_count, queue_count, pool_queue_count, 0, retry_at_ms, now_ms}
end

local sequence = redis.call("INCR", KEYS[6])
local ticket_expires_at = now_ms + queue_wait_ms
redis.call("ZADD", KEYS[2], "NX", sequence, member)
redis.call("ZADD", KEYS[3], "NX", ticket_expires_at, member)
redis.call("ZADD", KEYS[4], "NX", ticket_expires_at, member)
redis.call("ZADD", KEYS[5], "NX", ticket_expires_at, member)
refresh_queue_ttls()
queue_count = redis.call("ZCARD", KEYS[4])
pool_queue_count = redis.call("ZCARD", KEYS[2])
local rank = redis.call("ZRANK", KEYS[2], member)
return {2, active_count, queue_count, pool_queue_count, rank + 1, ticket_expires_at, now_ms}
